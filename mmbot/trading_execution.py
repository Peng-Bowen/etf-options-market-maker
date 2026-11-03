"""阶段三/四唯一发单入口。安全闸、联合压力检查与已核实准入不能由评分跳过。"""
import math
import uuid
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .apifacts import round_to_tick
from .config import MAX_ACTIVE_ORDERS, STALE_MS
from .probe_execution import ProbeBlocked, ProbeOrderManager, TERMINAL
from .risk import RiskChecker


class Admission:
    def __init__(self, probe_data, account_id, confirmations, now_ms):
        if probe_data.get('account_id') != account_id:
            raise ValueError('准入探针记录不是本账户')
        for key in ('valuation', 'quote_rule', 'recovery_test', 'risk_free_rate'):
            if not isinstance(confirmations.get(key), str) or not confirmations[key].strip():
                raise ValueError(f'缺少已核实事项的依据说明：{key}')
        today = datetime.fromtimestamp(now_ms/1000, ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')
        if today in probe_data.get('halted_days', []):
            raise ValueError('当日探针已触发停机，不允许用新策略重置')
        attempts = probe_data.get('attempts', {})
        trades = list(probe_data.get('trades', {}).values())
        p2, fees = False, []
        for a in attempts.values():
            matched = [t for t in trades if a.get('order_id') and t.get('order_id') == a['order_id']
                       and t.get('trade_date') == a['trade_date'] and t.get('transact_time') is not None
                       and t['transact_time'] <= now_ms and t.get('fee') is not None]
            if a['probe'] == 'P2' and a.get('market_crossed_ms'):
                p2 |= any(a['market_crossed_ms'] <= t['transact_time'] and t.get('liquidity') in ('MAKER','TAKER') for t in matched)
            if a['probe'] == 'P3':
                fees += [t['fee']/t['last_qty'] for t in matched if t.get('liquidity') == 'TAKER' and t['last_qty'] > 0 and t['fee'] >= 0]
        if not p2 or not fees or not all(math.isfinite(x) for x in fees):
            raise ValueError('需要 P2 触价角色证据及 P3 实际 TAKER 费用，不能用假定费率启动')
        self.taker_fee = max(fees)
        self.account_id = account_id
        self.confirmations = dict(confirmations)


class StrategyOrderManager(ProbeOrderManager):
    REF_PREFIX = 'mm_'

    def __init__(self,*args,**kwargs):
        self.rate_limit_count = 0
        super().__init__(*args,**kwargs)

    def _get(self,*args,**kwargs):
        response = super()._get(*args,**kwargs)
        if response is not None and response.status_code == 429:
            self.rate_limit_count += 1
        return response

    def _post(self,*args,**kwargs):
        response = super()._post(*args,**kwargs)
        if response is not None:
            from simtrade.utils.rest_tools import parse_json
            payload = parse_json(response) or {}
            if response.status_code == 429 or payload.get('code') == 14:
                self.rate_limit_count += 1
        return response

    def attach_strategy(self, engine, hard_limits, admission, ready, permitted, stage):
        self.engine, self.hard_limits, self.admission = engine, hard_limits, admission
        self.soft_limits = hard_limits
        self.ready, self.permitted, self.stage = ready, permitted, stage
        self.cooldown_until_ms = 0
        self.ledger_dirty = True
        self.fill_generation = 0

    def on_order(self, raw):
        known = next((a.get('cum_qty',0) for a in self.state.data['attempts'].values()
                      if raw.get('clientRef') == a['client_ref'] or
                      (a.get('order_id') and str(raw.get('orderId')) == a['order_id'])), 0)
        if raw.get('cumQty',0) > known:
            self.ledger_dirty = True
            self.fill_generation = getattr(self,'fill_generation',0)+1
        super().on_order(raw)

    def _risk(self, intents, reducing=False):
        joint = self._joint_portfolio()
        now_ms = int(self.now().timestamp()*1000)
        symbols = {p['securityId'] for p in joint.positions.values() if p.get('longQty',0) or p.get('shortQty',0)}
        symbols |= {o['productId'] for o in joint.active_orders} | {q.product_id for q in intents}
        values = {s:self.engine.value(s, now_ms) for s in symbols}
        candidates = [dict(productId=q.product_id,side=q.side,price=q.price,orderQty=q.qty,
                           cumQty=0,leavesQty=q.qty,ordStatus='0') for q in intents]
        limits = self.hard_limits if reducing else self.soft_limits
        fee = self.admission.taker_fee
        remaining = sum(o.get('leavesQty',o.get('orderQty',0)-o.get('cumQty',0)) for o in joint.active_orders)
        fee_reserve = fee*(remaining+sum(q.qty for q in intents))
        # available 已含系统冻结；再预留未知单和费用，宁可少报价，不提前花未确认资金。
        unknown_cash = sum(a['premium'] if a['side']=='1' else self.gate.products[a['product_id']]['sellMargin']*a['qty']
                           for a in self._risk_attempts().values() if a['status']=='unknown')
        available = joint.account.get('available')
        if available is None or not math.isfinite(available):
            raise ProbeBlocked('可用资金未知')
        if min(limits.max_loss-fee_reserve, limits.max_new_order_loss-fee*len(intents), available-unknown_cash-fee_reserve) < 0:
            raise ProbeBlocked('费用/未知订单预留后预算不足')
        limits = replace(limits, max_loss=limits.max_loss-fee_reserve,
                         max_new_order_loss=limits.max_new_order_loss-fee*len(intents),
                         available_cash=min(limits.available_cash, available-unknown_cash-fee_reserve))
        checker = RiskChecker(values, limits, now_ms,max_underlying_age_ms=self.engine.config.max_book_age_ms,
                              max_iv_age_ms=self.engine.config.max_iv_age_ms)
        ok, reasons = checker.check_hard_limits(list(joint.positions.values()), joint.active_orders, candidates)
        if not ok:
            raise ProbeBlocked('联合风险：'+', '.join(reasons))

    def _check(self, mode, intent):
        if mode not in ('MM','REDUCE') or type(intent.qty) is not int or intent.qty != 1:
            raise ProbeBlocked('正式策略仅允许 1 张报价或减仓')
        now = self.now()
        now_ms = int(now.timestamp()*1000)
        if (int(now.strftime('%Y%m%d')) != self.trade_date or not self.connected() or not self.ready()
                or self.ledger_dirty):
            raise ProbeBlocked('交易日、连接或账本尚未就绪')
        if now_ms < self.cooldown_until_ms:
            raise ProbeBlocked('服务端限流退避期间')
        if mode == 'MM' and not self.permitted(intent.product_id, now_ms):
            raise ProbeBlocked('合约没有通过当前阶段准入')
        if mode == 'MM' and self.stage == 4 and (now.hour,now.minute) >= (14,55):
            raise ProbeBlocked('阶段四 14:55 后只减仓')
        joint = self._joint_portfolio()
        errors = self.gate.check(intent, joint, now)
        self.remember_stop()
        if errors:
            raise ProbeBlocked(', '.join(errors))
        own_ids = {a.get('order_id') for a in self._risk_attempts().values() if a['status'] != 'conflict'}
        if any(o.get('orderId') not in own_ids for o in self.portfolio.active_orders):
            raise ProbeBlocked('账户存在非本策略活单')
        if any(a['status'] not in TERMINAL and a['product_id']==intent.product_id and a['side']==intent.side
               for a in self._risk_attempts().values()):
            raise ProbeBlocked('同侧仍有未结束/未知委托')
        book = self.market.books.get(intent.product_id)
        if book is None or not book.is_fresh(now_ms, STALE_MS):
            raise ProbeBlocked('交易 BBO 缺失或陈旧')
        if mode == 'MM':
            if ((intent.side=='1' and intent.price >= book.ask_px) or
                    (intent.side=='2' and intent.price <= book.bid_px)):
                raise ProbeBlocked('普通报价不得主动跨价')
            if not .9*book.bid_px <= intent.price <= 1.1*book.bid_px:
                raise ProbeBlocked('报价不在买一 ±10% 内')
        else:
            if joint.reducible_qty(intent.product_id,intent.side) < intent.qty:
                raise ProbeBlocked('减仓单缺少已核实的可平持仓')
            target = book.ask_px if intent.side=='1' else book.bid_px
            target = round_to_tick(target,self.gate.products[intent.product_id]['tickSize'],intent.side)
            if intent.price != target:
                raise ProbeBlocked('主动减仓仅按当前真实盘口限价')
        self._risk([intent], reducing=mode=='REDUCE')

    def validate_batch(self, intents, reducing=False):
        if not intents:
            return []
        mode = 'REDUCE' if reducing else 'MM'
        if len({(q.product_id,q.side) for q in intents}) != len(intents):
            raise ProbeBlocked('候选集合含同侧重复委托')
        if len(self._joint_portfolio().active_orders)+len(intents) > MAX_ACTIVE_ORDERS:
            raise ProbeBlocked('整批订单将超过活动委托上限')
        if self.gate.daily_orders_used+len(intents) > self.gate.limits.daily_cap:
            raise ProbeBlocked('整批订单超过日额度')
        for intent in intents:
            self._check(mode,intent)
        self._risk(intents,reducing=reducing)  # 不以逐笔通过代替集合通过。

    def submit_batch(self, intents, reducing=False):
        intents = list(intents)
        self.validate_batch(intents,reducing)
        mode = 'REDUCE' if reducing else 'MM'
        results = []
        for intent in intents:
            result = super().submit_probe('mm_'+uuid.uuid4().hex,mode,intent)
            results.append(result)
            self.journal.write('strategy_risk_checked',dict(client_ref=result['client_ref'],mode=mode))
            if self.last_code in (14,429):
                self.cooldown_until_ms = int(self.now().timestamp()*1000)+max(1000,int((self.retry_after or 1)*1000))
            if self.last_code in (401,403,25):
                self.gate.stopped = True; self.remember_stop()
            if result['status'] in ('unknown','rejected','conflict') or self.ledger_dirty:
                break
        return results

    def request_cancel(self, refs):
        refs = set(refs)
        for a in self.state.data['attempts'].values():
            if a['client_ref'] in refs and a['status'] not in TERMINAL:
                a['cancel_required'] = True
        self.state.save()
        self.expire()
