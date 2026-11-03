import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mmbot.apifacts import lots_to_units, round_to_tick, trade_push_time_to_epoch_ms
from mmbot.config import Config, Limits
from mmbot.execution import Intent, SafetyGate, ReadOnlyOrderManager, ReadOnlyError, QueryBudget
from mmbot.journal import Journal, replay
from mmbot.marketdata import MarketData
from mmbot.portfolio import Portfolio
from mmbot.refdata import load_universe


def me(**changes):
    data = dict(throttle_per_second=10, daily_order_limit=100, daily_orders_used=0,
                heavy_per_minute=120, http_per_second=30, http_per_minute=900,
                ws_max_connections=10, ws_connections=0, server_time=1793581200000,
                trade_date=20261102, api_trading_enabled=True)
    return dict(data, **changes)


def product(**changes):
    data = dict(productId='100001.SH', securityType='OPT', underlying='510050.SH',
                tickSize=0.0001, contractMultiplier=10000, strikePx=3.0,
                callOrPut='C', maturityDate=20261223, sellMargin=3000,
                priceLowerLimit=0.001, priceUpperLimit=1.0,
                buyQtyLowerLimit=1, buyQtyUpperLimit=100,
                sellQtyLowerLimit=1, sellQtyUpperLimit=100)
    return dict(data, **changes)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.state = Portfolio()
        self.state.account = dict(net=5_000_000, openFrozen=False)
        self.state.day_start_net = 5_000_000
        self.gate = SafetyGate(Limits.from_me(me()), {'100001.SH': product()})
        self.now = datetime(2026, 11, 2, 10, tzinfo=timezone.utc).astimezone()
        # 显式上海时间，测试不依赖宿主机时区。
        from zoneinfo import ZoneInfo
        self.now = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo('Asia/Shanghai'))

    def check(self, **kwargs):
        return self.gate.check(Intent('100001.SH', '1', .1, **kwargs), self.state, self.now)

    def test_valid_intent_is_only_preflight_not_permission_to_trade(self):
        self.assertEqual(self.check(qty=1), [])

    def test_single_order_limit_and_invalid_quantities(self):
        for qty in (6, 1.5, True, 0, -1):
            with self.subTest(qty=qty):
                self.assertIn('quantity', self.check(qty=qty))

    def test_active_limit_includes_unknown_status(self):
        for i in range(20):
            self.state.apply_order(dict(orderId=str(i), ordStatus='U', leavesQty=1))
        self.assertIn('active_orders', self.check(qty=1))

    def test_daily_limit_uses_account_usage_and_unlimited_has_local_cap(self):
        self.gate.daily_orders_used = 80
        self.assertIn('daily_orders', self.check(qty=1))
        self.assertEqual(Limits.from_me(me(daily_order_limit=0)).daily_cap, 2000)
        self.assertEqual(Limits.from_me(me(daily_order_limit=99)).daily_cap, 79)

    def test_shared_regular_and_emergency_bandwidth(self):
        for _ in range(7):
            self.assertTrue(self.gate.reserve_message(now=1.0))
        self.assertFalse(self.gate.reserve_message(now=1.1))
        for _ in range(3):
            self.assertTrue(self.gate.reserve_message(now=1.1, emergency=True))
        self.assertFalse(self.gate.reserve_message(now=1.1, emergency=True))
        self.assertFalse(self.gate.reserve_message(now=1.2))
        self.assertTrue(self.gate.reserve_message(now=2.2))

    def test_loss_stop_latches_even_after_net_recovers(self):
        self.state.account['net'] = 4_975_000
        self.assertIn('daily_loss', self.check(qty=1))
        self.state.account['net'] = 5_000_001
        self.assertIn('daily_loss', self.check(qty=1))

    def test_missing_baseline_is_not_assumed_to_be_startup_net(self):
        self.state.day_start_net = None
        self.assertIn('day_start_net_unknown', self.check(qty=1))

    def test_after_cutoff_cannot_claim_reduce_only(self):
        self.now = self.now.replace(hour=14, minute=57)
        self.assertIn('opening_forbidden', self.check(qty=1))
        self.state.positions['100001.SH'] = dict(shortQty=2, longQty=0, sellableQty=2)
        self.assertEqual(self.check(qty=2), [])
        self.state.apply_order(dict(orderId='old', productId='100001.SH', side='1',
                                    ordStatus='0', leavesQty=1))
        self.assertIn('opening_forbidden', self.check(qty=2))

    def test_open_frozen_requires_verified_reduction(self):
        self.state.account['openFrozen'] = True
        self.assertIn('opening_forbidden', self.check(qty=1))

    def test_whitelist_checks_refdata_not_symbol_appearance(self):
        self.gate.products['100001.SH'] = product(underlying='000300.SH')
        with self.assertRaises(ValueError):
            self.check(qty=1)
        for symbol in ('IF2612.CFE', '510050.SH', 'IO2612-C-4000.CFE'):
            with self.assertRaises(ValueError):
                self.gate.check(Intent(symbol, '1', .1, 1), self.state, self.now)

    def test_grid_limits_and_lunch(self):
        errors = self.gate.check(Intent('100001.SH', '1', .10005, 1), self.state, self.now)
        self.assertIn('price', errors)
        self.now = self.now.replace(hour=12)
        self.assertIn('session', self.check(qty=1))

    def test_readonly_blocks_all_sdk_trading_entrypoints(self):
        sdk = ReadOnlyOrderManager(logging.getLogger('test'), 'https://example.invalid/gateway')
        with patch.object(sdk, '_post', side_effect=AssertionError('network reached')):
            for method in (sdk.add_order, sdk.cancel_order, sdk.cancel_all):
                with self.assertRaises(ReadOnlyError):
                    method()


class DataTests(unittest.TestCase):
    def test_full_startup_with_sdk_and_fake_transport_makes_no_trade_requests(self):
        from mmbot.main import run
        from simtrade import MarketDataWebSocket, OrderWebSocket
        from simtrade.utils.rest_tools import RestTools
        calls = []

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, data):
                self.data = data

            def json(self):
                return dict(code=0, data=self.data)

        def get(url, *args, **kwargs):
            path = url.split('/gateway')[-1]
            calls.append(path)
            if path == '/me':
                # 真实 SDK 转换 camelCase，不能直接喂 snake_case 假装兼容。
                return Response(dict(throttlePerSecond=10, dailyOrderLimit=0, dailyOrdersUsed=0,
                                     heavyPerMinute=120, httpPerSecond=30, httpPerMinute=900,
                                     wsMaxConnections=10, wsConnections=0, serverTime=1793581200000,
                                     apiTradingEnabled=True, authType='API_KEY', tradeDate=20261102))
            if path == '/account':
                return Response(dict(net=5000000, available=5000000, openFrozen=False))
            if path == '/positions':
                return Response([])
            return Response(dict(items=[], page=0, hasMore=False, size=500))

        def post(url, *args, **kwargs):
            path = url.split('/gateway')[-1]
            calls.append(path)
            if path.startswith('/queryRefDataByUnderlying/'):
                underlying = path.rsplit('/', 1)[1]
                return Response([product(productId=underlying+'-opt', underlying=underlying)])
            if path.startswith('/queryRefData/'):
                return Response(dict(productId=path.rsplit('/', 1)[1], securityType='FUT', tickSize=.2))
            raise AssertionError('不应有账户写请求：'+path)

        def start(ws):
            ws.started = ws.logged_in = True
            ws.on_logged_in()

        with tempfile.TemporaryDirectory() as tmp:
            config = Config('https://example.invalid', 'ak_'+'x'*43,
                            ('IF2612.CFE', 'IH2612.CFE', 'IM2612.CFE', 'IC2612.CFE'), Path(tmp))
            with patch.object(RestTools, 'get_data', side_effect=get), patch.object(RestTools, 'post_data', side_effect=post), \
                 patch.object(MarketDataWebSocket, 'start', start), patch.object(OrderWebSocket, 'start', start), \
                 patch.object(MarketDataWebSocket, 'send_json'):
                result = run(config, .001)
            self.assertEqual(result['expected_symbols'], 12)
            self.assertTrue((Path(tmp)/'universe.json').exists())
            self.assertEqual(calls.count('/orders'), 1)
            self.assertEqual(calls.count('/trades'), 1)
            self.assertFalse(any('addOrder' in x or 'cancel' in x for x in calls))
            records = list(replay(next(Path(tmp).glob('events-*.jsonl'))))
            self.assertIn('shutdown', [r['kind'] for r in records])

    def test_runtime_uses_real_sdk_callbacks_and_splits_subscriptions(self):
        from mmbot.main import Recorder, subscription_groups
        from simtrade import MarketDataWebSocket, OrderWebSocket
        from mmbot.refdata import Universe
        with tempfile.TemporaryDirectory() as tmp:
            with Journal(Path(tmp)/'events.jsonl') as journal:
                recorder = Recorder(journal, Universe({'100001.SH': product()}, {}))
                md = MarketDataWebSocket(logging.getLogger('test'), 'ws://example.invalid/mdws',
                                         on_market_data=recorder.market_callback)
                orders = OrderWebSocket(logging.getLogger('test'), 'ws://example.invalid/orderws',
                                        on_order=recorder.callback('order'),
                                        on_trade=recorder.callback('trade_recover'))
                md.on_data(dict(code=0, channel='market', op='snapshot', data=dict(
                    productId='100001.SH', bidPx=.1, askPx=.1002, bidSize=1, askSize=1, timestamp=1234)))
                orders.on_data(dict(code=0, channel='trans.order', op='publish',
                                    data=[dict(orderId='a', ordStatus='1', cumQty=1, leavesQty=1)]))
                orders.on_data(dict(code=0, channel='trans.trade', op='recover', data=[dict(
                    execId='e', clOrdId='a', orderId='OTHER', transactionTime=20241119075718)]))
                recorder.drain()
                self.assertTrue(recorder.market.books['100001.SH'].is_valid)
                self.assertEqual(recorder.portfolio.active_orders[0]['cumQty'], 1)
            rows = list(replay(Path(tmp)/'events.jsonl'))
            self.assertEqual(rows[-1]['server_time_ms'], trade_push_time_to_epoch_ms(20241119075718))
        groups = subscription_groups(list(map(str, range(2001))), Limits.from_me(me()))
        self.assertEqual([len(x) for x in groups], [2000, 1])
        with self.assertRaises(ValueError):
            subscription_groups(['a'], Limits.from_me(me(ws_max_connections=1)))

    def test_sdk_get_budget_and_post_bypass_are_enforced(self):
        sdk = ReadOnlyOrderManager(logging.getLogger('test'), 'https://example.invalid/gateway')
        with self.assertRaises(ReadOnlyError):
            sdk._post('/addOrder', {})
        from unittest.mock import Mock
        sdk.budget = Mock()
        sdk.token = 'ak_' + 'x'*43
        sdk.api_trading_enabled = True
        with patch('simtrade.service.order_manager.RestTools.get_data', return_value=None), \
             self.assertLogs('test', level='WARNING'):
            sdk.query_positions()
        sdk.budget.acquire.assert_called_once_with(heavy=True)

    def test_time_units_and_directed_rounding(self):
        expected = int(datetime(2024, 11, 19, 7, 57, 18, tzinfo=timezone.utc).timestamp()*1000)
        self.assertEqual(trade_push_time_to_epoch_ms(20241119075718), expected)
        self.assertEqual(lots_to_units(product(), 2), 2)
        self.assertEqual(lots_to_units(dict(securityType='CS', buyRoundLot=100), 2), 200)
        with self.assertRaises(ValueError):
            lots_to_units(product(), True)
        self.assertEqual(round_to_tick(.10009, .0001, '1'), .1)
        self.assertEqual(round_to_tick(.10001, .0001, '2'), .1001)

    def test_no_quote_zero_timestamp_and_repeated_old_timestamp(self):
        md = MarketData()
        raw = dict(productId='100001.SH', bidPx=.1, askPx=.1002,
                   bidSize=3, askSize=4, last=.1001, timestamp=0)
        self.assertFalse(md.update(raw).is_valid)
        raw['timestamp'] = 1000
        book = md.update(raw)
        self.assertTrue(book.is_valid)
        raw['timestamp'] = 999
        self.assertIs(md.update(raw), book)
        self.assertIs(md.update(dict(raw, timestamp=1000)), book)
        self.assertFalse(book.is_fresh(now_ms=20000, max_age_ms=5000))

    def test_final_order_cannot_be_resurrected_by_old_snapshot(self):
        p = Portfolio()
        p.apply_order(dict(orderId='a', ordStatus='2', cumQty=1, transactionTime=200))
        p.apply_order(dict(orderId='a', ordStatus='0', cumQty=0, transactionTime=100))
        self.assertEqual(p.active_orders, [])
        p.apply_order(dict(orderId='b', ordStatus='X', cumQty=0))
        self.assertEqual(len(p.active_orders), 1)  # 查不到不等于没有风险。

    def test_journal_roundtrip_has_two_clocks_and_redacts_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'events.jsonl'
            with Journal(path, secret='ak_secret_full') as journal:
                journal.write('test', dict(token='ak_secret_full', text='key=ak_secret_full'), 123)
            record = list(replay(path))[0]
            self.assertEqual(record['server_time_ms'], 123)
            self.assertGreater(record['recv_monotonic'], 0)
            self.assertNotIn('ak_secret_full', path.read_text())

    def test_missing_me_limits_fail_instead_of_using_examples(self):
        data = me()
        del data['heavy_per_minute']
        with self.assertRaises(ValueError):
            Limits.from_me(data)

    def test_config_requires_concrete_four_futures_and_hides_key(self):
        env = dict(TRADING_GATEWAY_URL='https://school.example/gateway',
                   TRADING_API_KEY='ak_test',
                   TRADING_FUTURES='IF2612.CFE,IH2612.CFE,IM2612.CFE,IC2612.CFE')
        c = Config.from_env(env)
        self.assertEqual(c.md_url, 'wss://school.example/mdws')
        self.assertNotIn('ak_test', repr(c))
        with self.assertRaises(ValueError):
            Config.from_env(dict(env, TRADING_FUTURES='IF'))

    def test_universe_filters_index_options_and_checks_futures(self):
        class Ref:
            last_error = None

            def query_ref_data_by_underlying(self, underlying):
                return [product(productId=underlying+'-opt', underlying=underlying),
                        product(productId='IO2612.CFE', underlying='000300.SH')]

            def query_ref_data(self, symbol):
                return dict(productId=symbol, securityType='FUT', tickSize=.2)

        universe = load_universe(Ref(), ('IF2612.CFE', 'IH2612.CFE', 'IM2612.CFE', 'IC2612.CFE'))
        self.assertEqual(len(universe.options), 4)
        self.assertNotIn('IO2612.CFE', universe.symbols)
        self.assertEqual(len(universe.symbols), 12)

    def test_budget_respects_heavy_limit_separately(self):
        clock = [0.0]
        budget = QueryBudget(Limits.from_me(me(heavy_per_minute=1)),
                             clock=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0]+dt))
        budget.acquire(heavy=True)
        budget.acquire(heavy=False)
        self.assertEqual(clock[0], 0)
        budget.acquire(heavy=True)
        self.assertGreaterEqual(clock[0], 60)


if __name__ == '__main__':
    unittest.main()
