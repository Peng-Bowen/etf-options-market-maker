"""运行阶段 1：python -m mmbot.main --duration 1800。

需要 TRADING_GATEWAY_URL、TRADING_API_KEY、TRADING_FUTURES。
SDK 从学生端下载的本地目录加入 PYTHONPATH，禁止使用公共同名包。
"""
import argparse
import copy
import json
import logging
import math
import queue
import signal
import threading
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from simtrade import MarketDataWebSocket, OrderWebSocket, RefDataManager

from .apifacts import MAX_SUBSCRIPTIONS, trade_push_time_to_epoch_ms
from .config import Config, Limits, EVENT_QUEUE_SIZE, STALE_MS
from .execution import ReadOnlyOrderManager, QueryBudget, SafetyGate
from .journal import Journal, SecretFilter, redact
from .marketdata import MarketData
from .portfolio import Portfolio
from .refdata import Universe, load_universe

LOG = logging.getLogger('mmbot')


def subscription_groups(symbols: list, limits: Limits) -> list:
    groups = [symbols[i:i+MAX_SUBSCRIPTIONS] for i in range(0, len(symbols), MAX_SUBSCRIPTIONS)]
    if limits.ws_connections + len(groups) + 1 > limits.ws_max_connections:
        raise ValueError('账户剩余 WebSocket 连接数不足以订阅全集和订单流')
    return groups


class Recorder:
    """SDK 线程只入队；账本更新和落盘在主线程，避免跨回调竞争。"""
    def __init__(self, journal: Journal, universe: Universe):
        self.journal, self.universe = journal, universe
        self.market, self.portfolio = MarketData(), Portfolio()
        self.events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.overflow = threading.Event()
        self.closed = threading.Event()
        self.count = 0
        self.multi_tick_jumps = 0
        self.listeners = []

    def callback(self, kind):
        def receive(payload, op):
            if self.closed.is_set():
                return
            event = (kind, copy.deepcopy(payload), op, time.monotonic(), int(time.time()*1000))
            try:
                self.events.put_nowait(event)
            except queue.Full:
                self.overflow.set()  # 采集不完整即报错，不静默丢弃。
        return receive

    def market_callback(self, op, payload):
        self.callback('market')(payload, op)

    def drain(self, max_events=10000):
        for _ in range(max_events):
            try:
                kind, raw, op, mono, wall = self.events.get_nowait()
            except queue.Empty:
                break
            self.count += 1
            server_time = None
            if kind == 'market':
                server_time = raw.get('timestamp') or None
                previous = self.market.books.get(raw['productId'])
                current = self.market.update(raw, mono)
                ref = self.universe.options.get(raw['productId'], {})
                tick = ref.get('tickSize')
                if previous and current is not previous and previous.is_valid and current.is_valid and tick:
                    jump = abs(current.bid_px-previous.bid_px)/tick
                    if jump > 1.5:
                        self.multi_tick_jumps += 1
                        self.journal.write('multi_tick_jump', dict(product_id=current.product_id,
                                           bid_jump_ticks=jump, note='快照跳变，不能推断成交或缺失条数'),
                                           server_time, mono, wall)
            elif kind == 'order':
                self.portfolio.apply_order(raw)
                server_time = raw.get('transactionTime') or raw.get('transactTime')
            elif kind == 'account':
                self.portfolio.account = dict(raw)
            elif kind == 'position':
                # A28：推送没有 sellableQty；旧查询值不继续视为可平证明。
                self.portfolio.positions[raw['securityId']] = dict(raw, sellableQty=None)
            elif kind == 'trade_recover':
                value = raw.get('transactionTime')
                server_time = trade_push_time_to_epoch_ms(value) if value else None
            self.journal.write(kind, dict(op=op, data=raw), server_time, mono, wall)
            for listener in tuple(self.listeners):
                listener(kind, raw, op, mono, wall)
        if self.overflow.is_set():
            raise RuntimeError('事件队列已满，本次采集不完整，请检查磁盘/消费速度')


def require_result(value, sdk, name):
    if value is None:
        raise RuntimeError(f'{name} 失败：{sdk.last_error}')
    return value


def run(config: Config, duration_seconds: float = 1800, probe_plan=None, risk_config=None, strategy_settings=None) -> dict:
    """默认只读；显式传入已校验 probe_plan 时运行阶段二。duration=0 持续运行。"""
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError('duration 必须为非负有限秒数')
    if strategy_settings is not None and (probe_plan is not None or risk_config is None):
        raise ValueError('正式策略与探针模式互斥，且必须提供风控配置')
    config.data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-%f')
    path = config.data_dir / f'events-{stamp}.jsonl'
    stop = threading.Event()
    old_handlers = {}
    sockets = []
    sdk = None
    probes = None
    valuation_monitor = None
    strategy = None
    summary = {}
    handler = logging.FileHandler(config.data_dir / f'sdk-{stamp}.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    handler.addFilter(SecretFilter(config.api_key))
    logging.getLogger().addHandler(handler)
    try:
        with Journal(path, secret=config.api_key) as journal:
            recorder = Recorder(journal, Universe({}, {}))
            ows = OrderWebSocket(LOG, config.order_url, on_order=recorder.callback('order'),
                                on_account=recorder.callback('account'), on_position=recorder.callback('position'),
                                on_trade=recorder.callback('trade_recover'))
            # 先读取账户限额和合约，再开连接，避免在确认连接数之前越限。
            md_sockets = []
            # SDK 3.0.1 参数虽名为 websockets，实际只接收单个对象。
            # 订单连接交 SDK 管理；行情分片连接在本模块统一启停。
            manager_class = ReadOnlyOrderManager
            if probe_plan is not None:
                from .probe_execution import ProbeOrderManager
                manager_class = ProbeOrderManager
            elif strategy_settings is not None:
                from .trading_execution import StrategyOrderManager
                manager_class = StrategyOrderManager
            sdk = manager_class(LOG, config.gateway_url, None, ows,
                                       api_key=config.api_key, auto_start_websockets=False)
            try:
                if not sdk.login():
                    raise RuntimeError(f'API Key 验证失败：{sdk.last_error}')
                info = require_result(sdk.me(), sdk, '/me')
                limits = Limits.from_me(info)
                if not info.get('api_trading_enabled'):
                    raise RuntimeError('账户未启用 API 查询/交易权限，请在学生端核对')
                server_time = info.get('server_time')
                if not server_time:
                    raise ValueError('/me 未返回服务器时间')
                journal.server_offset_ms = server_time - int(time.time()*1000)
                journal.write('me', info, server_time)
                LOG.info('实读限额：%s', json.dumps(asdict(limits), ensure_ascii=False))
                sdk.budget = QueryBudget(limits)
                # 已完成的登录和 me 也计入初始窗口，避免边界上的额外突发。
                sdk.budget.acquire()
                sdk.budget.acquire()
                ref = RefDataManager(LOG, config.gateway_url)
                universe = load_universe(ref, config.futures, sdk.budget)
                universe.save(config.data_dir / 'universe.json')
                recorder.universe = universe
                if risk_config is not None:
                    from .valuation_monitor import ValuationMonitor
                    pricing_config,risk_limits,comparison=risk_config
                    valuation_monitor=ValuationMonitor(recorder,pricing_config,risk_limits,
                                                       greek_atol=comparison['atol'],greek_rtol=comparison['rtol'])
                groups = subscription_groups(universe.symbols, limits)
                journal.write('universe', dict(options=len(universe.options), futures=list(universe.futures),
                                              subscriptions=len(universe.symbols)))
                account = require_result(sdk.query_account(), sdk, 'account')
                positions = require_result(sdk.query_positions(), sdk, 'positions')
                orders = require_result(sdk.query_orders(page=0), sdk, 'orders')  # A8，包含所有活单。
                trades = require_result(sdk.query_trades(page=0), sdk, 'trades')  # 只读初始页，非完整归因。
                for name, value in (('account', account), ('positions', positions), ('orders_page0', orders), ('trades_page0', trades)):
                    journal.write('initial_'+name, value)
                recorder.portfolio.account = dict(account['raw'])
                recorder.portfolio.day_start_net = config.day_start_net
                recorder.portfolio.positions = {p['security_id']: dict(p['raw']) for p in positions}
                raw_orders = [o['raw'] for o in orders['items']]
                for raw in raw_orders:
                    recorder.portfolio.apply_order(raw)
                ows.apply_reconciled(raw_orders)  # SDK 断线对账需要知道初始化时的活单。
                gate = SafetyGate(limits, universe.options)
                ows.add_recover_listener(lambda reconnected, truncated: recorder.callback('order_recovery')(
                    dict(reconnected=reconnected, truncated=truncated), 'recover'))
                for group in groups:
                    md = MarketDataWebSocket(LOG, config.md_url, on_market_data=recorder.market_callback)
                    md.subscribe_md_by_symbol(group)
                    md_sockets.append(md)
                sockets = [ows] + md_sockets
                if probe_plan is not None:
                    from .probe import ProbeSession
                    probes = ProbeSession(probe_plan, sdk, recorder, gate, info, config,
                                          connected=lambda: bool(sockets) and all(ws.logged_in for ws in sockets)
                                          and ows.login_count == 1)
                if strategy_settings is not None:
                    from .strategy import StrategySession
                    strategy = StrategySession(strategy_settings,sdk,recorder,valuation_monitor,gate,info,config,
                        connected=lambda: bool(sockets) and all(ws.logged_in for ws in sockets) and ows.login_count == 1)
                for ws in sockets:
                    ws.set_token(sdk.token)
                    ws.start()
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
                started = time.monotonic()
                next_health = started
                last_issues = None
                mode = f'阶段{strategy_settings.stage}策略' if strategy else '阶段二探针' if probes else '只读采集'
                LOG.info('%s启动：%d 期权，%d 行情合约；日志 %s', mode,
                         len(universe.options), len(universe.symbols), path)
                while not stop.is_set():
                    recorder.drain()
                    now = time.monotonic()
                    if any(ws.auth_error for ws in sockets):
                        raise RuntimeError('WebSocket 鉴权失效，请检查学生端 API Key 状态')
                    issues = gate.observe_net(recorder.portfolio.account.get('net'), recorder.portfolio.day_start_net)
                    if issues != last_issues:
                        journal.write('safety_state', dict(issues=issues, stage=mode))
                        last_issues = issues
                    if probes is not None:
                        probes.step()
                    if strategy is not None:
                        strategy.step()
                    if valuation_monitor is not None:
                        server_now=int(time.time()*1000)+journal.server_offset_ms
                        if server_now-valuation_monitor.last_snapshot_ms>=1000:
                            valuation_monitor.snapshot(server_now)
                    if now >= next_health:
                        server_now = int(time.time()*1000) + journal.server_offset_ms
                        fresh = sum(b.is_fresh(server_now, STALE_MS) for b in recorder.market.books.values())
                        summary = dict(elapsed_seconds=round(now-started, 2), events=recorder.count,
                                       received_symbols=len(recorder.market.books), expected_symbols=len(universe.symbols),
                                       fresh_two_sided_books=fresh, multi_tick_jumps=recorder.multi_tick_jumps,
                                       connections_logged_in=[bool(ws.logged_in) for ws in sockets],
                                       order_login_count=ows.login_count, active_account_orders=len(recorder.portfolio.active_orders),
                                       rejected_subscriptions=sorted(set().union(*(md.rejected_symbols for md in md_sockets))))
                        journal.write('health', summary)
                        LOG.info('采集状态：%s', json.dumps(summary, ensure_ascii=False))
                        next_health = now+30
                    journal.flush()
                    if duration_seconds and now-started >= duration_seconds:
                        break
                    stop.wait(.25)  # 仅消费队列/检查退出，不触发行情轮询或报价。
                summary.update(elapsed_seconds=round(time.monotonic()-started, 2), events=recorder.count)
                journal.write('shutdown', summary)
            finally:
                try:
                    if strategy is not None:
                        try:
                            strategy.shutdown()
                        finally:
                            strategy.close()
                    if probes is not None:
                        try:
                            probes.shutdown()
                        finally:
                            probes.close()
                finally:
                    for md in md_sockets:
                        md.stop()
                    if sdk is not None:
                        sdk.logout()  # API Key 模式只关闭连接，不调用登出接口。
                    recorder.closed.set()
                    recorder.drain(max_events=EVENT_QUEUE_SIZE)
                    journal.flush()
    finally:
        for ws in sockets:
            ws.stop()
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)
        logging.getLogger().removeHandler(handler)
        handler.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=1800, help='运行秒数，0 为持续运行；默认 1800')
    parser.add_argument('--probe-plan', type=Path, help='阶段二 JSON 计划路径')
    parser.add_argument('--execute-probes', action='store_true', help='明确启用阶段二试单；未指定时保持只读')
    parser.add_argument('--risk-config',type=Path,help='阶段三只读定价和压力检查配置，不启用报价')
    parser.add_argument('--strategy-config',type=Path,help='阶段三/四正式策略配置')
    parser.add_argument('--execute-strategy',action='store_true',help='显式启用正式策略交易，须配合 --strategy-config 和 --risk-config')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = None
    try:
        if bool(args.probe_plan) != args.execute_probes:
            raise ValueError('执行探针须同时指定 --probe-plan 和 --execute-probes')
        if bool(args.strategy_config) != args.execute_strategy:
            raise ValueError('正式交易须同时指定 --strategy-config 和 --execute-strategy')
        if args.strategy_config and (args.probe_plan or not args.risk_config):
            raise ValueError('正式策略与探针模式互斥，且必须提供 --risk-config')
        strategy_settings=None
        if args.strategy_config:
            from .strategy import load_strategy_config
            strategy_settings=load_strategy_config(args.strategy_config)
        plan = None
        risk_config=None
        if args.risk_config:
            from .valuation_monitor import load_risk_config
            risk_config=load_risk_config(args.risk_config)
        if args.probe_plan:
            from .probe import load_plan
            plan = load_plan(args.probe_plan)
        config = Config.from_env()
        for handler in logging.getLogger().handlers:
            handler.addFilter(SecretFilter(config.api_key))
        run(config, args.duration, probe_plan=plan,risk_config=risk_config,strategy_settings=strategy_settings)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, redact(str(exc), config.api_key if config else '') + '\n')


if __name__ == '__main__':
    main()
