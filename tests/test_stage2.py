import logging
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_stage1 import product, me
from mmbot.config import Limits
from mmbot.execution import Intent, ReadOnlyError, SafetyGate
from mmbot.marketdata import MarketData
from mmbot.portfolio import Portfolio
from mmbot.journal import Journal
from mmbot.probe_state import ProbeState
from mmbot.probe_execution import ProbeOrderManager, ProbeBlocked
from mmbot.probe import probe_intent, load_plan, write_report
from mmbot.attribution import TradeCollector

NOW = datetime(2026, 11, 2, 10, tzinfo=ZoneInfo('Asia/Shanghai'))


class Response:
    status_code = 200
    headers = {}

    def __init__(self, code=0, data=None):
        self.code, self.data = code, data

    def json(self):
        return dict(code=self.code, data=self.data)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        # 所有单元测试默认截断传输；即使安全闸回归，也绝不碰网络。
        transport = patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(19))
        transport.start()
        self.addCleanup(transport.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = ProbeState(self.root/'state.json', account_id=1)
        self.journal = Journal(self.root/'events.jsonl')
        self.portfolio = Portfolio()
        self.portfolio.account = dict(net=5_000_000, available=5_000_000, openFrozen=False)
        self.portfolio.day_start_net = 5_000_000
        self.md = MarketData()
        self.md.update(dict(productId='100001.SH', bidPx=.01, askPx=.0102,
                            bidSize=10, askSize=10, timestamp=int(NOW.timestamp()*1000)))
        self.gate = SafetyGate(Limits.from_me(me()), {'100001.SH': product()})
        self.sdk = ProbeOrderManager(logging.getLogger('probe-test'), 'https://example.invalid/gateway',
                                     client_throttle=False, retry_on_throttle=0)
        self.sdk.token = 'ak_'+'x'*43
        self.sdk.api_trading_enabled = True
        self.sdk.configure(self.gate, self.portfolio, self.md, self.state, self.journal,
                           trade_date=20261102, premium_budget=500, now=lambda: NOW, connected=lambda: True)

    def tearDown(self):
        self.journal.close()
        self.state.close()
        self.tmp.cleanup()

    def submit(self, probe='P2', side='1', price=.0099, qty=1, key='one'):
        return self.sdk.submit_probe(key, probe, Intent('100001.SH', side, price, qty))

    def test_prices_use_reference_tick_and_direction(self):
        book = self.md.books['100001.SH']
        self.assertEqual(probe_intent('P2', book, product()).price, .0099)
        self.assertEqual(probe_intent('P3', book, product()).price, .0102)
        self.assertEqual(probe_intent('P4', book, product()).price, .01)
        self.assertEqual(probe_intent('P5_SELL', book, product()).price, .015)
        self.assertEqual(probe_intent('P5_BUY', book, product()).price, .005)

    def test_sdk_bypass_and_bad_quantity_never_reach_network(self):
        from simtrade import OrderManager
        with patch('simtrade.service.order_manager.RestTools.post_data') as post:
            with self.assertRaises(ReadOnlyError):
                self.sdk.add_order('100001.SH', '1', '2', .01, 1)
            with self.assertRaises(ReadOnlyError):
                OrderManager.add_order(self.sdk, '100001.SH', '1', '2', .01, 1)
            with self.assertRaises(ProbeBlocked):
                self.submit(qty=2)
            post.assert_not_called()

    def test_uncertain_attempt_is_persisted_and_never_duplicated(self):
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {'orderId':'a'})) as post:
            attempt = self.submit()
            self.assertEqual(attempt['status'], 'unknown')
            with self.assertRaises(ProbeBlocked):
                self.submit()
            self.assertEqual(post.call_count, 1)
            persisted = self.state.data['attempts']['one']
            self.assertEqual(post.call_args.args[1]['clientRef'], persisted['client_ref'])
            self.assertEqual(self.gate.daily_orders_used, 1)

    def test_daily_loss_stale_book_and_naked_sell_block(self):
        with self.assertRaises(ProbeBlocked):
            self.submit(probe='P5_SELL', side='2', price=.015)
        self.portfolio.account['net'] = 4_975_000
        with self.assertRaises(ProbeBlocked):
            self.submit()
        self.gate.stopped = False
        self.portfolio.account['net'] = 5_000_000
        self.md.update(dict(productId='100001.SH', timestamp=0))
        with self.assertRaises(ProbeBlocked):
            self.submit()

    def test_premium_budget_counts_unknown_pending_buys(self):
        self.sdk.premium_budget = 100
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {})):
            self.submit()
            with self.assertRaises(ProbeBlocked):
                self.submit(key='second')

    def test_cancel_timeout_and_late_id_need_final_push(self):
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {})):
            a = self.submit()
        self.sdk.expire(now_ms=a['sent_ms']+120001)
        self.assertTrue(a['cancel_required'])
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(0)) as post:
            self.sdk.on_order(dict(orderId='late', clientRef=a['client_ref'], tradeDate=20261102,
                                   productId='100001.SH', side='1', ordStatus='0', cumQty=0, leavesQty=1))
            self.sdk.expire(now_ms=a['sent_ms']+121000)
            self.assertTrue(post.call_args.args[0].endswith('/cancelOrderAsync'))
            self.assertNotIn(a['status'], ('cancelled', 'filled'))
        self.sdk.on_order(dict(orderId='late', clientRef=a['client_ref'], tradeDate=20261102,
                               ordStatus='4', cumQty=0, leavesQty=0))
        self.assertEqual(a['status'], 'cancelled')

    def test_code25_is_not_owned_and_foreign_cancel_blocked(self):
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(25, {'orderId':'foreign'})):
            a = self.submit()
        self.assertEqual(a['status'], 'conflict')
        with self.assertRaises(ReadOnlyError):
            self.sdk.cancel_order('foreign')

    def test_http_500_remains_unknown_and_accepts_late_working_push(self):
        response = Response(500)
        response.status_code = 500
        with patch('simtrade.service.order_manager.RestTools.post_data',return_value=response):
            attempt = self.submit()
        self.assertEqual(attempt['status'],'unknown')
        self.sdk.on_order(dict(orderId='late500',clientRef=attempt['client_ref'],tradeDate=20261102,
                               productId='100001.SH',side='1',ordStatus='0',cumQty=0,leavesQty=1))
        self.assertEqual(attempt['status'],'working')

    def test_documented_http_503_is_not_submitted(self):
        response = Response(503)
        response.status_code = 503
        with patch('simtrade.service.order_manager.RestTools.post_data',return_value=response):
            attempt = self.submit()
        self.assertEqual(attempt['status'],'rejected')

    def test_baseline_and_lock_survive_restart(self):
        self.state.baseline(20261102, 5000000)
        with self.assertRaises(ValueError):
            self.state.baseline(20261102, 4990000)
        with self.assertRaises(RuntimeError):
            ProbeState(self.root/'state.json', account_id=1)
        self.state.close()
        self.state = ProbeState(self.root/'state.json', account_id=1)
        self.assertEqual(self.state.baseline(20261102, None), 5000000)

    def test_loss_halt_survives_restart_even_if_net_recovers(self):
        self.portfolio.account['net'] = 4_975_000
        with self.assertRaises(ProbeBlocked): self.submit()
        self.portfolio.account['net'] = 5_000_000
        other_gate = SafetyGate(Limits.from_me(me()), {'100001.SH': product()})
        self.sdk.configure(other_gate, self.portfolio, self.md, self.state, self.journal,
                           20261102, 500, lambda: NOW, lambda: True)
        with self.assertRaises(ProbeBlocked): self.submit(key='restart')

    def test_existing_long_exposure_counts_toward_probe_budget(self):
        self.portfolio.positions['existing'] = dict(longQty=1, shortQty=0, positionValue=600)
        with self.assertRaises(ProbeBlocked): self.submit()

    def test_trade_queries_do_not_wait_and_consume_cancel_reserve(self):
        from mmbot.execution import QueryBudget
        budget = QueryBudget(Limits.from_me(me(http_per_second=3)))
        self.sdk.budget = budget
        self.sdk.budget.acquire()
        self.sdk.budget.acquire()
        with patch('simtrade.service.order_manager.RestTools.get_data') as get:
            with self.assertRaises(ProbeBlocked): self.sdk.query_trades()
            get.assert_not_called()
        self.assertTrue(budget.acquire(block=False))

    def test_trade_pages_deduplicate_and_allow_late_fee(self):
        collector = TradeCollector(self.sdk, self.state, self.journal)
        row = dict(exec_id='t', trade_date=20261102, order_id='a', last_qty=1,
                   last_px=.01, transact_time=123, liquidity=None, fee=None)
        with patch.object(self.sdk, 'query_trades', side_effect=[
            dict(items=[row], has_more=True), dict(items=[row], has_more=False),
            dict(items=[dict(row, liquidity='MAKER', fee=-1.8)], has_more=False)]):
            collector.poll(20261102)
            collector.poll(20261102)
            collector.poll(20261102)
        self.assertEqual(len(self.state.data['trades']), 1)
        self.assertEqual(next(iter(self.state.data['trades'].values()))['fee'], -1.8)

    def test_report_does_not_invent_p2_or_p6_conclusions(self):
        write_report(self.root/'report.md', self.state.data)
        report = (self.root/'report.md').read_text()
        self.assertIn('证据不足', report)
        self.assertIn('P6', report)

    def test_p6_three_scheduled_snapshots_include_next_day_without_trading(self):
        from mmbot.main import Recorder
        from mmbot.refdata import Universe
        from mmbot.probe import ProbeSession
        from mmbot.config import Config
        path = self.root/'p6.json'
        times = ['2026-11-02T14:55:00+08:00', '2026-11-02T14:59:00+08:00', '2026-11-03T09:20:00+08:00']
        path.write_text(json.dumps(dict(trade_date=20261102, observations=[dict(label=label, at=at)
                        for label,at in zip(('close_1455','close_1459','next_open'), times)])))
        recorder = Recorder(self.journal, Universe({'100001.SH':product()}, {}))
        recorder.portfolio = self.portfolio
        session = ProbeSession(load_plan(path), self.sdk, recorder, self.gate, me(account_id=8),
                               Config('https://example.invalid','key',(),self.root,5000000), lambda:True)
        account = dict(net=5000000, raw=dict(net=5000000, available=5000000, openFrozen=False))
        position = dict(security_id='100001.SH', raw=dict(securityId='100001.SH', longQty=1, shortQty=0,
                         marginPrice=.01, positionValue=100, sellableQty=1))
        try:
            with patch.object(self.sdk, 'query_account', return_value=account) as query, \
                 patch.object(self.sdk, 'query_positions', return_value=[position]), \
                 patch.object(self.sdk, 'query_trades', return_value=dict(items=[], has_more=False)), \
                 patch.object(self.sdk, 'submit_probe') as submit:
                for at in times:
                    session.now = lambda at=at: datetime.fromisoformat(at)
                    session.step()
                    session.step()
                self.assertEqual(query.call_count, 3)
                submit.assert_not_called()
                self.assertEqual(len(session.state.data['observations']), 3)
                self.assertEqual(session.state.data['observations']['20261102:next_open']['positions'][0]['raw']['marginPrice'], .01)
        finally:
            session.close()

    def test_empty_rejection_does_not_override_known_fill(self):
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {'orderId':'a'})):
            a = self.submit()
        self.sdk.on_order(dict(orderId='a', clientRef=a['client_ref'], ordStatus='2', cumQty=1))
        self.sdk.on_order(dict(orderId='a', clientRef=a['client_ref'], ordStatus='0', cumQty=0))
        self.assertEqual(a['status'], 'filled')

    def test_p5_sell_cannot_overclose_two_pending_sells(self):
        self.portfolio.positions['100001.SH'] = dict(longQty=1, shortQty=0, sellableQty=1)
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {})):
            self.submit(probe='P5_SELL', side='2', price=.015)
            with self.assertRaises(ProbeBlocked):
                self.submit(probe='P5_SELL', side='2', price=.015, key='second')

    def test_price_decimal_matches_probe_generation(self):
        self.md.update(dict(productId='100001.SH', bidPx=.0123, askPx=.0125,
                            bidSize=10, askSize=10, timestamp=int(NOW.timestamp()*1000)+1))
        # 测试时间向后 1 ms，避免未来快照。
        from datetime import timedelta
        self.sdk.now = lambda: NOW+timedelta(milliseconds=1)
        intent = probe_intent('P2', self.md.books['100001.SH'], product())
        with patch('simtrade.service.order_manager.RestTools.post_data', return_value=Response(22, {})):
            self.sdk.submit_probe('decimal', 'P2', intent)

    def test_plan_rejects_duplicate_and_bad_p6_dates(self):
        path = self.root/'plan.json'
        item = dict(id='a', probe='P2', product_id='100001.SH', at='10:00:00')
        path.write_text(json.dumps(dict(trade_date=20261102, attempts=[item, item])))
        with self.assertRaises(ValueError):
            load_plan(path)
        path.write_text(json.dumps(dict(trade_date=20261102, observations=[dict(label='next_open', at='2026-11-02T09:20:00+08:00')])))
        with self.assertRaises(ValueError):
            load_plan(path)

    def test_all_seven_gates_rechecked_at_execution_boundary(self):
        from dataclasses import replace
        cases = ['quantity', 'active', 'daily', 'rate', 'loss', 'cutoff', 'whitelist']
        for case in cases:
            with self.subTest(case=case):
                old_products = dict(self.gate.products)
                self.portfolio.orders.clear()
                self.portfolio.account['net'] = 5_000_000
                self.gate.stopped = False
                self.gate.daily_orders_used = 0
                self.gate._messages.clear()
                self.sdk.now = lambda: NOW
                qty = 1
                if case == 'quantity': qty = 6
                if case == 'active':
                    for n in range(20): self.portfolio.orders[str(n)] = dict(orderId=str(n), ordStatus='U')
                if case == 'daily': self.gate.daily_orders_used = 80
                if case == 'rate':
                    for _ in range(7): self.gate.reserve_message()
                if case == 'loss': self.portfolio.account['net'] = 4_975_000
                if case == 'cutoff':
                    later = NOW.replace(hour=14, minute=57)
                    self.sdk.now = lambda: later
                    self.md.update(dict(productId='100001.SH', bidPx=.01, askPx=.0102, bidSize=1, askSize=1,
                                        timestamp=int(later.timestamp()*1000)))
                if case == 'whitelist': self.gate.products['100001.SH'] = product(underlying='000300.SH')
                with patch('simtrade.service.order_manager.RestTools.post_data') as post:
                    with self.assertRaises(ProbeBlocked):
                        self.submit(qty=qty, key=case)
                    post.assert_not_called()
                self.gate.products = old_products
                self.md.books.clear()
                self.md.update(dict(productId='100001.SH', bidPx=.01, askPx=.0102, bidSize=1, askSize=1,
                                    timestamp=int(NOW.timestamp()*1000)))


class StageTwoIntegration(unittest.TestCase):
    def test_plan_to_actual_sdk_fill_trade_poll_report_and_shutdown(self):
        from mmbot.main import run
        from mmbot.config import Config
        from simtrade import MarketDataWebSocket, OrderWebSocket
        from simtrade.utils.rest_tools import RestTools
        calls, submitted, sockets = [], {}, {}
        stamp = int(NOW.timestamp()*1000)

        def get(url, *args, **kwargs):
            path = url.split('/gateway')[-1]
            calls.append(path)
            if path == '/me':
                return Response(data=dict(throttlePerSecond=10, dailyOrderLimit=100, dailyOrdersUsed=0,
                    heavyPerMinute=120, httpPerSecond=30, httpPerMinute=900, wsMaxConnections=10,
                    wsConnections=0, serverTime=stamp, apiTradingEnabled=True, authType='API_KEY',
                    tradeDate=20261102, accountId=7))
            if path == '/account':
                return Response(data=dict(net=5000000, available=5000000, openFrozen=False))
            if path == '/positions': return Response(data=[])
            trades = []
            if path == '/trades' and submitted:
                trades = [dict(execId='fill', tradeDate=20261102, orderId='order', productId='100001.SH',
                               lastQty=1, lastPx=.01, fee=-1.8, liquidity='MAKER', transactTime=stamp+10)]
            return Response(data=dict(items=trades, hasMore=False, page=0, size=500))

        def post(url, payload, *args, **kwargs):
            path = url.split('/gateway')[-1]
            calls.append(path)
            if path.startswith('/queryRefDataByUnderlying/'):
                underlying = path.rsplit('/', 1)[1]
                return Response(data=[product(productId='100001.SH' if underlying=='510050.SH' else underlying+'opt', underlying=underlying)])
            if path.startswith('/queryRefData/'):
                return Response(data=dict(productId=path.rsplit('/', 1)[1], securityType='FUT', tickSize=.2))
            if path == '/addOrder':
                submitted.update(payload)
                raw = dict(payload, orderId='order', tradeDate=20261102, ordStatus='2', cumQty=1, leavesQty=0,
                           transactionTime=stamp+10)
                raw.pop('token', None)
                sockets['order'].on_data(dict(code=0, channel='trans.order', op='publish', data=[raw]))
                return Response(data=raw)
            raise AssertionError('意外请求：'+path)

        def start_order(ws):
            sockets['order'] = ws
            ws.started = ws.logged_in = True
            ws.on_logged_in()
            ws.on_data(dict(code=0, channel='trans.order', op='recover', data=[]))

        def start_md(ws):
            ws.started = ws.logged_in = True
            ws.on_data(dict(code=0, channel='market', op='snapshot', data=dict(productId='100001.SH',
                           bidPx=.01, askPx=.0102, bidSize=2, askSize=2, timestamp=stamp)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planfile = root/'plan.json'
            planfile.write_text(json.dumps(dict(trade_date=20261102, attempts=[dict(
                id='one', probe='P4', product_id='100001.SH', at='10:00:00')])))
            config = Config('https://example.invalid', 'ak_'+'x'*43,
                            ('IF2612.CFE','IH2612.CFE','IM2612.CFE','IC2612.CFE'), root, 5000000)
            with patch.object(RestTools, 'get_data', side_effect=get), patch.object(RestTools, 'post_data', side_effect=post), \
                 patch.object(OrderWebSocket, 'start', start_order), patch.object(MarketDataWebSocket, 'start', start_md):
                run(config, .3, load_plan(planfile))
            state = json.loads((root/'probe-state-7.json').read_text())
            self.assertEqual(state['attempts']['20261102:one']['status'], 'filled')
            self.assertEqual(calls.count('/addOrder'), 1)
            self.assertEqual(calls.count('/orders'), 1)
            self.assertIn('MAKER', (root/'probe_report.md').read_text())
            self.assertEqual(submitted['orderQty'], 1)
            self.assertIn('P4', submitted['note'])


if __name__ == '__main__':
    unittest.main()
