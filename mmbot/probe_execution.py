"""阶段二执行边界：全部 1 张，禁止裸卖，未知结果与撤单待确认仍占额度。"""
import copy
import threading
import time
import uuid
import math
from decimal import Decimal
from datetime import datetime

from simtrade import OrderManager

from .apifacts import FINAL_STATUSES, round_to_tick
from .config import STALE_MS
from .execution import ReadOnlyOrderManager, ReadOnlyError
from .order_recovery import OrderRecoveryMixin

TERMINAL = frozenset(('filled', 'cancelled', 'rejected', 'conflict', 'not_found'))
TIMEOUT_MS = 120000


class ProbeBlocked(RuntimeError):
    """安全条件不满足；不应以新 clientRef 自动重试同一任务。"""


class ProbeOrderManager(OrderRecoveryMixin, ReadOnlyOrderManager):
    REF_PREFIX = 'p2_'
    def __init__(self, *args, **kwargs):
        kwargs['client_throttle'] = False  # 由执行闸统一预算，不在 SDK 内等待而阻塞撤单。
        kwargs['retry_on_throttle'] = 0
        super().__init__(*args, **kwargs)

    def configure(self, gate, portfolio, market, state, journal, trade_date, premium_budget,
                  now, connected):
        if not 0 < premium_budget <= 5000:
            raise ValueError('探针累计权利金预算须在 (0, 5000] 元内')
        self.gate, self.portfolio, self.market = gate, portfolio, market
        self.state, self.journal, self.trade_date = state, journal, trade_date
        self.premium_budget, self.now, self.connected = premium_budget, now, connected
        self._authorized = None
        self.retry_on_throttle = 0  # 每次请求自行占用额度，不让 SDK 暗中重发。
        self._submit_lock = threading.Lock()
        if str(trade_date) in state.data.setdefault('halted_days', []):
            gate.stopped = True
        self.gate.daily_orders_used = max(gate.daily_orders_used, state.data['daily_used'].get(str(trade_date), 0))
        # 重启后的未知单只等待查证/撤单，绝不在新交易日重放旧 ref。
        for attempt in state.data['attempts'].values():
            if attempt['status'] not in TERMINAL:
                attempt['cancel_required'] = True
        state.save()

    def _get(self, path, *args, **kwargs):
        running = hasattr(self, 'state')
        if self.budget is not None and not self.budget.acquire(
                heavy=path.split('?')[0] in ('/positions', '/orders', '/trades'),
                block=not running, reserve_http=running):
            raise ProbeBlocked('查询预算暂不可用，留出应急撤单带宽')
        return OrderManager._get(self, path, *args, **kwargs)

    def remember_stop(self):
        if self.gate.stopped:
            days = self.state.data.setdefault('halted_days', [])
            if str(self.trade_date) not in days:
                days.append(str(self.trade_date))
                self.state.save()

    def _post(self, path, payload, extra_headers=None):
        authorization = getattr(self, '_authorized', None)
        actual = {k: v for k, v in payload.items() if k != 'token'}
        if authorization != (threading.get_ident(), path, actual):
            raise ReadOnlyError('账户写请求必须来自探针执行闸')
        self._authorized = None  # 单次许可，不能被 SDK 的隐式重试复用。
        if self.budget is not None and not self.budget.acquire(block=False):
            raise ProbeBlocked('HTTP 带宽暂不可用')
        if not self.gate.reserve_message(emergency=path != '/addOrder'):
            raise ProbeBlocked('报撤带宽暂不可用')
        return OrderManager._post(self, path, payload, extra_headers)

    def _joint_portfolio(self):
        joint = copy.deepcopy(self.portfolio)
        for key, a in self._risk_attempts().items():
            if a['status'] in TERMINAL:
                continue
            order_id = a.get('order_id')
            if order_id and order_id in joint.orders:
                continue
            joint.orders['pending:'+key] = dict(productId=a['product_id'], side=a['side'],
                price=a['price'],orderQty=a['qty'],cumQty=0,leavesQty=a['qty'],
                clientRef=a['client_ref'],ordStatus='A')
        return joint

    def _check(self, probe, intent):
        now = self.now()
        if int(now.strftime('%Y%m%d')) != self.trade_date:
            raise ProbeBlocked('不跨交易日执行旧探针计划')
        if not self.connected():
            raise ProbeBlocked('行情/订单连接尚未恢复')
        if intent.qty != 1 or type(intent.qty) is not int:
            raise ProbeBlocked('阶段二每笔严格限定 1 张')
        book = self.market.books.get(intent.product_id)
        if not book or not book.is_fresh(int(now.timestamp()*1000), STALE_MS):
            raise ProbeBlocked('行情缺失或陈旧')
        joint = self._joint_portfolio()
        try:
            errors = self.gate.check(intent, joint, now)
        except ValueError as exc:
            raise ProbeBlocked(str(exc)) from exc
        self.remember_stop()
        if errors:
            raise ProbeBlocked(', '.join(errors))
        # 再核对探针价格，调用方不能把探针执行器当任意交易入口。
        tick = self.gate.products[intent.product_id]['tickSize']
        bid, ask, step = Decimal(str(book.bid_px)), Decimal(str(book.ask_px)), Decimal(str(tick))
        values = {'P2': ('1', bid-step), 'P3': ('1', ask),
                  'P4': ('1', bid), 'P5_BUY': ('1', bid*Decimal('.5')),
                  'P5_SELL': ('2', bid*Decimal('1.5'))}
        if probe not in values:
            raise ProbeBlocked('未知探针')
        side, price = values[probe]
        if intent.side != side or intent.price != round_to_tick(float(price), tick, side):
            raise ProbeBlocked('委托与探针价格规则不符')
        own_ids = {a.get('order_id') for a in self._risk_attempts().values() if a['status'] != 'conflict'}
        if any(o.get('orderId') not in own_ids for o in self.portfolio.active_orders):
            raise ProbeBlocked('账户存在非本探针活单，不能安全隔离联合敞口')
        if any(p.get('shortQty', 0) > 0 for p in joint.positions.values()):
            raise ProbeBlocked('阶段二不承接已有空头组合')
        if intent.side == '2' and joint.reducible_qty(intent.product_id, '2') < 1:
            raise ProbeBlocked('P5 卖单仅允许平已有多头')
        if probe.startswith('P5') and sum(a['probe'].startswith('P5') and a['trade_date'] == self.trade_date
                                        for a in self._risk_attempts().values()) >= 2:
            raise ProbeBlocked('P5 每日最多两次尝试，拒单也计入')
        if intent.side == '1':
            cost = intent.price * self.gate.products[intent.product_id]['contractMultiplier']
            committed = sum(a['premium'] for a in self._risk_attempts().values()
                            if a['trade_date'] == self.trade_date and a['status'] not in ('rejected', 'conflict','not_found'))
            if committed + cost > self.premium_budget:
                raise ProbeBlocked('累计买入权利金预算不足（未知单同样占用）')
            held = 0.
            for p in joint.positions.values():
                if p.get('longQty', 0) <= 0:
                    continue
                value = p.get('positionValue')
                if value is None or not math.isfinite(value) or value < 0:
                    raise ProbeBlocked('已有多头价值未知，无法核对联合风险上界')
                held += value
            pending = sum(a['premium'] for a in self._risk_attempts().values() if a['status'] not in TERMINAL)
            if held+pending+cost > self.premium_budget:
                raise ProbeBlocked('已有持仓与未完成买单的联合权利金超过探针预算')
            available = joint.account.get('available')
            if available is None or cost > available:
                raise ProbeBlocked('可用资金不足或未知')

    def submit_probe(self, key, probe, intent):
        with self._submit_lock:
            if key in self.state.data['attempts']:
                raise ProbeBlocked('任务已尝试；结果未知不得换 ref 重下')
            self._check(probe, intent)
            now_ms = int(self.now().timestamp()*1000)
            ref = self.REF_PREFIX + uuid.uuid4().hex[:20]
            book = self.market.books[intent.product_id]
            attempt = dict(probe=probe, product_id=intent.product_id, side=intent.side,
                           price=intent.price, qty=1, client_ref=ref, trade_date=self.trade_date,
                           sent_ms=now_ms, sent_monotonic=time.monotonic(), status='unknown',
                           order_id=None, cum_qty=0, cancel_required=False,
                           premium=intent.price*self.gate.products[intent.product_id]['contractMultiplier']
                           if intent.side == '1' else 0.,
                           book_at_submit=dict(bid=book.bid_px, ask=book.ask_px, timestamp=book.timestamp_ms))
            self.state.data['attempts'][key] = attempt
            attempt['request'] = self._request_payload(attempt)
            self.gate.daily_orders_used += 1  # A16：保守计数，拒单/未知结果不退回。
            self.state.data['daily_used'][str(self.trade_date)] = self.gate.daily_orders_used
            self.state.save()  # 写前日志：进程在 HTTP 响应前崩溃也不会重复下单。
            note = f'{probe} 开盘探针 1 张' if probe.startswith('P') else f'{probe} 策略委托 1 张'
            payload = dict(productId=intent.product_id, side=intent.side, ordType='2',
                           price=intent.price, orderQty=1, timeInForce='0', clientRef=ref, note=note)
            self.journal.write('probe_submit', dict(key=key, **attempt), now_ms)
            self.journal.flush()
            self.retry_on_throttle = 0
            self._authorized = (threading.get_ident(), '/addOrder', payload)
            try:
                result = OrderManager.add_order(self, intent.product_id, intent.side, '2', intent.price, 1,
                                                '0', client_ref=ref, note=payload['note'])
                self._record_order_response(attempt,result)
                self.journal.write('probe_response', dict(key=key, result=result, code=self.last_code,
                                                        error=self.last_error))
            except ProbeBlocked as exc:
                # _post 在进入传输前拒绝，本次确定未发送，不制造永远未知的孤儿单。
                attempt['status'] = 'rejected'
                attempt['local_block'] = str(exc)
                self.journal.write('probe_local_not_sent', dict(key=key, reason=str(exc)))
                raise
            finally:
                self._authorized = None
                self.state.save()
            return attempt

    def _record_order_response(self, attempt, result, is_retry=False):
        attempt['response_code'] = self.last_code
        attempt['response'] = copy.deepcopy(result)
        attempt['response_error'] = self.last_error
        if self.last_code == 25:
            attempt['status'] = 'conflict'
            self.gate.stopped = True
            self.remember_stop()
        elif result is not None:
            attempt['order_id'] = result.get('order_id') or attempt.get('order_id')
            if not result.get('processing') and result.get('order_id'):
                raw = dict(productId=attempt['product_id'],side=attempt['side'],orderQty=attempt['qty'],
                           leavesQty=max(0,attempt['qty']-result.get('cum_qty',0)),price=attempt['price'])
                raw.update(result.get('raw') or {})
                raw.update(orderId=result['order_id'],clientRef=attempt['client_ref'],tradeDate=attempt['trade_date'],
                           ordStatus=result.get('ord_status'),cumQty=result.get('cum_qty',0))
                self.on_order(raw)
        elif not is_retry:
            unknown = (self.last_code in (None,20,22,24,502,504)
                       or (500 <= self.last_code < 600 and self.last_code != 503))
            if not unknown:
                attempt['status'] = 'rejected'

    def on_order(self, raw):
        if raw.get('ordStatus') not in ('0','1','2','4','8'):
            for a in self.state.data['attempts'].values():
                matches = raw.get('clientRef') == a['client_ref'] or (a.get('order_id') and str(raw.get('orderId')) == a['order_id'])
                if a['status'] == 'not_found' and matches:
                    return  # SDK 本地 A/U/X 不是服务器迟到回报，不能重建已对账的幽灵敞口。
        self.portfolio.apply_order(raw)
        for a in self.state.data['attempts'].values():
            if a['status'] == 'conflict':
                continue
            match = raw.get('clientRef') == a['client_ref'] or (a.get('order_id') and str(raw.get('orderId')) == a['order_id'])
            if not match or raw.get('tradeDate', a['trade_date']) != a['trade_date']:
                continue
            if a['status'] in ('filled', 'cancelled', 'rejected') and raw.get('ordStatus') not in FINAL_STATUSES:
                continue
            if int(raw.get('cumQty', 0)) < a['cum_qty']:
                continue
            a['order_id'] = str(raw['orderId'])
            if raw.get('ordStatus') in ('0','1','2','4','8'):
                a.pop('absence_first_ms',None)
            a['cum_qty'] = int(raw.get('cumQty', 0))
            a['order'] = dict(raw)
            a['status'] = {'2':'filled', '4':'cancelled', '8':'rejected', '0':'working', '1':'partial'}.get(raw.get('ordStatus'), 'unknown')
            self.journal.write('probe_order', dict(client_ref=a['client_ref'], raw=raw),
                               raw.get('transactionTime') or raw.get('transactTime'))
            self.state.save()

    def expire(self, now_ms=None, all_orders=False):
        """A7：逐笔撤本状态文件拥有的单。接收撤单成功响应不释放敞口。"""
        now_ms = int(self.now().timestamp()*1000) if now_ms is None else now_ms
        if all_orders:
            for a in self.state.data['attempts'].values():
                if a['status'] not in TERMINAL:
                    a['cancel_required'] = True
        self.recover_unknown(now_ms,stopping=all_orders)
        dirty = False
        for a in self.state.data['attempts'].values():
            if a['status'] in TERMINAL:
                continue
            if all_orders or now_ms >= a['sent_ms'] + TIMEOUT_MS or time.monotonic()-a['sent_monotonic'] >= TIMEOUT_MS/1000:
                dirty |= not a['cancel_required']
                a['cancel_required'] = True
            if not a['cancel_required'] or not a.get('order_id') or now_ms < a.get('cancel_after_ms', 0):
                continue
            payload = dict(orderId=a['order_id'])
            dirty = True
            self.retry_on_throttle = 0
            self._authorized = (threading.get_ident(), '/cancelOrderAsync', payload)
            try:
                accepted = OrderManager.cancel_order(self, a['order_id'], async_cancel=True)
                a['cancel_after_ms'] = now_ms + max(3000, int((self.retry_after or 0)*1000))
                self.journal.write('probe_cancel', dict(client_ref=a['client_ref'], accepted=accepted,
                                                       code=self.last_code, error=self.last_error))
            except ProbeBlocked:
                a['cancel_after_ms'] = now_ms+1000
            finally:
                self._authorized = None
        if dirty:
            self.state.save()
