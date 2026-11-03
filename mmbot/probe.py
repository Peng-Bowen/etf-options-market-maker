"""P1–P6 探针计划、状态机与证据报告；无预测模型或成交概率假设。"""
import json
import copy
import math
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .apifacts import round_to_tick
from .attribution import TradeCollector
from .execution import Intent
from .config import Limits
from .probe_execution import ProbeBlocked, TERMINAL
from .probe_state import ProbeState

SHANGHAI = ZoneInfo('Asia/Shanghai')
PROBES = ('P2', 'P3', 'P4', 'P5_SELL', 'P5_BUY')


def probe_intent(probe, book, product):
    tick = Decimal(str(product['tickSize']))
    bid, ask = Decimal(str(book.bid_px)), Decimal(str(book.ask_px))
    prices = {'P2': ('1', bid-tick), 'P3': ('1', ask), 'P4': ('1', bid),
              'P5_SELL': ('2', bid*Decimal('1.5')), 'P5_BUY': ('1', bid*Decimal('.5'))}
    side, price = prices[probe]
    return Intent(book.product_id, side, round_to_tick(float(price), float(tick), side), 1)


def load_plan(path: Path) -> dict:
    return parse_plan(json.loads(path.read_text(encoding='utf-8')))


def parse_plan(raw: dict) -> dict:
    plan = copy.deepcopy(raw)
    date = datetime.strptime(str(plan['trade_date']), '%Y%m%d').replace(tzinfo=SHANGHAI)
    budget = plan.get('premium_budget', 500)
    if isinstance(budget, bool) or not math.isfinite(budget) or not 0 < budget <= 5000:
        raise ValueError('premium_budget 必须在 (0, 5000] 元内')
    plan['premium_budget'] = budget
    attempts = plan.setdefault('attempts', [])
    if not isinstance(attempts, list) or len(attempts) > 20:
        raise ValueError('每份探针计划最多 20 项')
    keys = set()
    p5 = 0
    for item in attempts:
        if item['probe'] not in PROBES or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', item['id']):
            raise ValueError('探针类型或 id 无效')
        if not isinstance(item.get('product_id'), str) or not item['product_id']:
            raise ValueError('必须明确指定期权合约代码')
        key = f"{plan['trade_date']}:{item['id']}"
        if key in keys:
            raise ValueError('同日任务 id 不可重复')
        keys.add(key)
        clock = datetime.strptime(item['at'], '%H:%M:%S').time()
        at = datetime.combine(date.date(), clock, SHANGHAI)
        if not ('09:30:00' <= item['at'] < '11:30:00' or '13:00:00' <= item['at'] < '14:57:00'):
            raise ValueError('探针启动时间须在交易时段且早于 14:57')
        item['key'], item['at_ms'] = key, int(at.timestamp()*1000)
        p5 += item['probe'].startswith('P5')
    if p5 > 2:
        raise ValueError('P5 最多两次，分别测试买卖方向即可')
    observations = plan.setdefault('observations', [])
    labels = set()
    for item in observations:
        label = item['label']
        if label not in ('close_1455', 'close_1459', 'next_open') or label in labels:
            raise ValueError('P6 label 必须唯一且为 close_1455/close_1459/next_open')
        labels.add(label)
        at = datetime.fromisoformat(item['at'])
        if at.tzinfo is None:
            raise ValueError('P6 时间须包含时区，例如 +08:00')
        at = at.astimezone(SHANGHAI)
        if label.startswith('close_'):
            expected = '14:55' if label == 'close_1455' else '14:59'
            if at.date() != date.date() or at.strftime('%H:%M') != expected:
                raise ValueError('P6 收盘快照须安排在计划交易日的对应分钟')
        elif at.date() <= date.date() or not '09:00' <= at.strftime('%H:%M') < '09:30':
            raise ValueError('next_open 须明确安排在后续交易日的 09:00–09:30')
        item['key'] = f"{plan['trade_date']}:{label}"
        item['at_ms'] = int(at.timestamp()*1000)
    return plan


def write_report(path: Path, data: dict):
    """报告只陈述证据，P2 无可归因成交绝不自动通过验收。"""
    rows = ['# 阶段二探针报告', '', '本报告由实际记录生成；未观测到不等于机制不存在。', '',
            '## P1 实际限额', '', '```json', json.dumps(data.get('limits', {}), ensure_ascii=False, indent=2), '```', '',
            '## P2–P5 委托与成交', '',
            '|任务|探针|状态|成交张数|角色|实际 fee（元）|成交时延 ms|结论|',
            '|---|---|---|---:|---|---|---|---|']
    p2_confirmed = False
    for key, a in data['attempts'].items():
        trades = [t for t in data['trades'].values() if t.get('order_id') == a.get('order_id')
                  and t.get('trade_date') == a['trade_date']]
        qty = sum(t['last_qty'] for t in trades)
        roles = sorted({t.get('liquidity') or '未知' for t in trades})
        fee = sum(t['fee'] for t in trades) if trades and all(t.get('fee') is not None for t in trades) else None
        order_time = a.get('order', {}).get('orderTime') or a['sent_ms']
        delays = [t['transact_time']-order_time for t in trades if t.get('transact_time') is not None]
        conclusion = '证据不足，等待成交查询或市场条件'
        if a['status'] == 'cancelled' and not qty and not a.get('cum_qty'):
            conclusion = '超时/停机撤单已确认，未成交；不能判断成交角色'
        if trades:
            conclusion = '记录成交角色和实际费用；不外推所有合约'
        if a['probe'] == 'P2':
            cross = a.get('market_crossed_ms')
            qualifying = [t for t in trades if cross and t.get('transact_time') and cross <= t['transact_time']
                          and t.get('liquidity') in ('MAKER', 'TAKER')]
            if qualifying:
                p2_confirmed = True
                conclusion = '已观察到盘口触价后成交，角色：'+','.join(sorted({t['liquidity'] for t in qualifying}))+'；L1 无法证明唯一撮合路径'
            else:
                conclusion = '证据不足：需盘口触价记录及对应成交角色'
        elif a['probe'] == 'P3' and trades:
            first = a.get('next_market_ms')
            conclusion = f'下一次收到的行情时间={first}；查询成交时间='+','.join(str(t.get('transact_time')) for t in trades)
        elif a['probe'].startswith('P5'):
            conclusion = ('该远价委托被受理；不能证明评分规则不限制' if a.get('order_id') and a['status'] in ('working', 'partial', 'filled', 'cancelled')
                          else '拒单/未确定：需结合原始错误判断，不能自动归因为 ±10%')
        rows.append(f"|{key}|{a['probe']}|{a['status']}|{qty}|{','.join(roles) or '未知'}|{fee if fee is not None else '未知'}|{','.join(map(str, delays)) or '未知'}|{conclusion}|")
    rows += ['', 'P2 结论：'+('有符合条件的观测样本，仍需跨合约/时段复核。' if p2_confirmed else '证据不足，阶段二现场验收未通过。'), '',
             '时延优先使用服务端 orderTime；缺失时使用按 /me 校准的本地下单时间估计，不能当作精确撮合耗时。', '',
             '## 未执行项', '', '```json', json.dumps(data.get('skipped', {}), ensure_ascii=False, indent=2), '```', '',
             '## P6 日终估值与入账', '',
             '仅对照系统 net、marginPrice、positionValue 与已查询 fee；快照本身不足以证明结算公式或返佣准确入账时刻。', '']
    waiting = [k for k in data.get('planned', {}) if k not in data['attempts'] and k not in data.get('skipped', {})]
    if waiting:
        rows += ['尚未尝试的计划项：'+', '.join(waiting), '']
    if not data.get('observations'):
        rows.append('证据不足：尚无定时账户/持仓快照。')
    for key, obs in data.get('observations', {}).items():
        rows += [f'### {key}', '', '```json', json.dumps(obs, ensure_ascii=False, indent=2), '```', '']
    unknown = [k for k,a in data['attempts'].items() if a['status'] not in TERMINAL]
    rows += ['', '未确认结束的委托：'+(', '.join(unknown) or '无'), '',
             '成交后持仓可能仍在账户中；本程序不自动市价清仓，P6 可继续观察这些持仓。']
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(rows)+'\n', encoding='utf-8')
    temporary.replace(path)


class ProbeSession:
    def __init__(self, plan, sdk, recorder, gate, info, config, connected):
        self.plan, self.sdk, self.recorder, self.gate = plan, sdk, recorder, gate
        self.config, self.connected = config, connected
        self.state = ProbeState(config.data_dir/f"probe-state-{info['account_id']}.json", info['account_id'])
        self.last_poll = self.last_report = 0.
        self.halting = False
        self.account_date = info['trade_date']
        try:
            current = int(self.now().strftime('%Y%m%d'))
            if plan['attempts'] and plan['trade_date'] != current:
                raise ValueError('交易探针计划日期不是服务器当前日期；跨日 P6 请使用仅含 observations 的计划')
            self.state.data['limits'][str(info['trade_date'])] = dict(info)
            self.state.data.setdefault('planned', {}).update({item['key']: dict(item) for item in plan['attempts']})
            baseline = self.state.baseline(info['trade_date'], config.day_start_net)
            if plan['attempts'] and baseline is None:
                raise ValueError('交易探针必须提供 TRADING_DAY_START_NET；不能用盘中启动净值代替')
            recorder.portfolio.day_start_net = baseline
            sdk.configure(gate, recorder.portfolio, recorder.market, self.state, recorder.journal,
                          plan['trade_date'], plan['premium_budget'], self.now, connected)
            # 用初始化查询恢复旧进程订单；configure 已将旧未完成委托标记待撤。
            for raw in list(recorder.portfolio.orders.values()):
                sdk.on_order(raw)
            self.collector = TradeCollector(sdk, self.state, recorder.journal)
            self.state.save()
            recorder.listeners.append(self.on_event)
        except Exception:
            self.state.close()
            raise

    def now(self):
        offset = self.recorder.journal.server_offset_ms or 0
        return datetime.fromtimestamp((time.time()*1000+offset)/1000, SHANGHAI)

    def on_event(self, kind, raw, op, mono, wall):
        if kind == 'order':
            self.sdk.on_order(raw)
        elif kind == 'market':
            changed = False
            for a in self.state.data['attempts'].values():
                if a['product_id'] != raw.get('productId') or mono <= a['sent_monotonic']:
                    continue
                stamp = raw.get('timestamp') or 0
                if stamp <= a['book_at_submit']['timestamp'] or stamp > a['sent_ms']+120000:
                    continue
                if not a.get('next_market_ms'):
                    a['next_market_ms'] = stamp
                    changed = True
                if not a.get('market_crossed_ms') and 0 < raw.get('askPx', 0) <= a['price']:
                    a['market_crossed_ms'] = stamp
                    changed = True
            if changed:
                self.state.save()

    def refresh_account(self, before_order=False):
        """仅在发单前/定时 P6 查询；不周期轮询持仓或委托。"""
        if before_order:
            info = self.sdk.me()
            if info is None or info.get('trade_date') != self.plan['trade_date'] or not info.get('api_trading_enabled'):
                raise ProbeBlocked('账户权限或交易日无法核实')
            limits = Limits.from_me(info)
            self.gate.limits = limits
            self.sdk.budget.limits = limits
            self.gate.daily_orders_used = max(self.gate.daily_orders_used, limits.daily_orders_used)
        account = self.sdk.query_account()
        positions = self.sdk.query_positions()
        if account is None or positions is None:
            raise ProbeBlocked('账户/可平持仓查询失败')
        self.recorder.portfolio.account = dict(account['raw'])
        self.recorder.portfolio.positions = {p['security_id']: dict(p['raw']) for p in positions}
        self.recorder.journal.write('probe_account_refresh', dict(account=account, positions=positions))
        return account, positions

    def step(self):
        now = self.now()
        now_ms = int(now.timestamp()*1000)
        issues = self.gate.observe_net(self.recorder.portfolio.account.get('net'), self.recorder.portfolio.day_start_net)
        self.sdk.remember_stop()
        if self.gate.stopped or int(now.strftime('%Y%m%d')) != self.plan['trade_date']:
            self.halting = True
        self.sdk.expire(now_ms, all_orders=self.halting or not self.connected())
        # 定时器只用于到期撤单/快照/成交查询；下单须有新行情才考虑。
        if not self.halting and not issues and self.connected():
            for item in self.plan['attempts']:
                key = item['key']
                if key in self.state.data['attempts'] or key in self.state.data['skipped'] or now_ms < item['at_ms']:
                    continue
                if now_ms > item['at_ms']+120000:
                    self.state.data['skipped'][key] = item.get('_last_reason', '错过启动窗口/始终没有可用的新行情')
                    self.state.save()
                    continue
                book = self.recorder.market.books.get(item['product_id'])
                if not book or book.timestamp_ms < item['at_ms'] or book.timestamp_ms <= item.get('_last_book', 0):
                    continue
                item['_last_book'] = book.timestamp_ms
                try:
                    self.refresh_account(before_order=True)
                    # 查询期间可能已有订单/行情推送，先入账再检查。
                    self.recorder.drain()
                    book = self.recorder.market.books[item['product_id']]
                    intent = probe_intent(item['probe'], book, self.gate.products[item['product_id']])
                    self.sdk.submit_probe(key, item['probe'], intent)
                except (ProbeBlocked, ValueError, KeyError) as exc:
                    self.recorder.journal.write('probe_blocked', dict(key=key, reason=str(exc)))
                    item['_last_reason'] = str(exc)
                break  # 每次循环最多一次新增尝试，先处理撤单和回报。
        for item in self.plan['observations']:
            if item['key'] in self.state.data['observations'] or item['key'] in self.state.data['skipped'] or now_ms < item['at_ms']:
                continue
            if now_ms >= item['at_ms']+60000:
                self.state.data['skipped'][item['key']] = '错过 P6 指定分钟，未用迟到数据冒充'
                self.state.save()
                continue
            try:
                account, positions = self.refresh_account()
                capture_ms = int(self.now().timestamp()*1000)
                observation = dict(scheduled_ms=item['at_ms'], captured_ms=capture_ms,
                                   within_minute=capture_ms < item['at_ms']+60000,
                                   net=account['net'], account=account, positions=positions,
                                   books={k:vars(v) for k,v in self.recorder.market.books.items() if k in self.recorder.portfolio.positions})
                self.state.data['observations'][item['key']] = observation
                self.recorder.journal.write('P6_snapshot', observation)
                self.state.save()
            except ProbeBlocked as exc:
                self.recorder.journal.write('P6_query_failed', dict(key=item['key'], reason=str(exc)))
        mono = time.monotonic()
        if mono-self.last_poll >= max(5., 120/self.gate.limits.heavy_per_minute):
            self.last_poll = mono
            # 固定查询计划交易日，支持次日继续补齐前一天成交费用。
            self.collector.poll(self.plan['trade_date'])
        if mono-self.last_report >= 5:
            self.report()
            self.last_report = mono

    def report(self):
        write_report(self.config.data_dir/'probe_report.md', self.state.data)

    def shutdown(self, seconds=15):
        """有限等待取消确认；未确认状态落盘并清晰报告，不伪报撤单完成。"""
        self.halting = True
        deadline = time.monotonic()+seconds
        try:
            while True:
                self.recorder.drain()
                self.sdk.expire(all_orders=True)
                pending = [a for a in self.state.data['attempts'].values() if a['status'] not in TERMINAL]
                if not pending or time.monotonic() >= deadline:
                    break
                time.sleep(.25)
            self.collector.poll(self.plan['trade_date'])
            self.report()
        finally:
            self.state.save()

    def close(self):
        self.recorder.listeners.remove(self.on_event)
        self.state.close()
