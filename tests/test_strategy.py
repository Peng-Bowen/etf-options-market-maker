import tempfile
import unittest
import logging
import json
import time
from unittest.mock import patch
from pathlib import Path
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import test_pricing_risk as pricing_tests
from test_pricing_risk import NOW, book
from test_stage1 import product, me
from mmbot.execution import Intent, SafetyGate
from mmbot.config import Limits
from mmbot.quoter import QuotePolicy, make_quotes, order_diff
from mmbot.edge import EdgeTracker
from mmbot.coverage import CoveragePolicy, CoverageController
from mmbot.trading_execution import Admission, StrategyOrderManager
from mmbot.probe_execution import ProbeBlocked, TERMINAL
from mmbot.probe_state import ProbeState
from mmbot.journal import Journal
from mmbot.portfolio import Portfolio
from mmbot.risk import RiskLimits
from test_stage2 import Response


class QuoterTests(unittest.TestCase):
    def setUp(self):
        helper = pricing_tests.PricingTests()
        self.engine = helper.engine()
        helper.seed(self.engine)
        self.value = self.engine.value('100001.SH', NOW)
        self.book = book('100001.SH', self.value.price)
        self.policy = QuotePolicy(half_spread_ticks=1, reprice_ticks=2, min_rest_ms=3000)

    def test_quotes_passive_grid_and_one_lot(self):
        quotes = make_quotes(self.value, self.book, product(), self.policy)
        self.assertEqual(len(quotes), 2)
        self.assertLess(quotes[0].price, self.book.ask_px)
        self.assertGreater(quotes[1].price, self.book.bid_px)
        self.assertTrue(all(q.qty == 1 for q in quotes))

    def test_diff_keeps_orders_until_age_and_cancel_confirmation(self):
        desired = [Intent('100001.SH', '1', .11, 1)]
        live = dict(client_ref='a', product_id='100001.SH', side='1', price=.1,
                    qty=1, status='working', sent_ms=NOW, cancel_required=False)
        cancel, add = order_diff(desired, [live], {'100001.SH': product()}, self.policy, NOW+100)
        self.assertEqual((cancel, add), ([], []))
        cancel, add = order_diff(desired, [live], {'100001.SH': product()}, self.policy, NOW+4000)
        self.assertEqual(cancel, ['a']); self.assertEqual(add, [])
        live['cancel_required'] = True
        self.assertEqual(order_diff(desired, [live], {'100001.SH': product()}, self.policy, NOW+5000)[1], [])
        live['status'] = 'cancelled'
        self.assertEqual(order_diff(desired, [live], {'100001.SH': product()}, self.policy, NOW+6000)[1], desired)

    def test_unknown_order_blocks_duplicate_side(self):
        a = dict(client_ref='a', product_id='100001.SH', side='1', price=.1, qty=1,
                 status='unknown', sent_ms=NOW, cancel_required=False)
        _, additions = order_diff([Intent('100001.SH','1',.1,1)], [a], {'100001.SH':product()}, self.policy,NOW+1000)
        self.assertEqual(additions, [])


class EdgeTests(unittest.TestCase):
    def setUp(self):
        self.data = dict(trades={}, attempts={}, edge={}, fair_history={})
        self.tracker = EdgeTracker(self.data, taker_fee_per_lot=2., fair_tolerance_ms=5000)

    def trade(self, stamp=NOW, fee=-1.8):
        self.data['attempts']['a'] = dict(order_id='o', status='filled', trade_date=20261102)
        self.data['trades']['t'] = dict(exec_id='t',order_id='o',trade_date=20261102,product_id='100001.SH',
                                       side='1',last_qty=1,last_px=.1,fee=fee,liquidity='MAKER',transact_time=stamp)

    def test_exact_decomposition_and_late_trade_query(self):
        self.tracker.add_fair('100001.SH', NOW-1, .1002, .1001, .1003, 10000)
        self.tracker.add_fair('100001.SH', NOW+60000, .1001, .1, .1002, 10000)
        self.trade()
        self.tracker.refresh(NOW+61000)
        row = self.data['edge']['t']
        self.assertAlmostEqual(row['spread'], 2.)
        self.assertAlmostEqual(row['markout'], -1.)
        self.assertAlmostEqual(row['rebate'], 1.8)
        self.assertAlmostEqual(row['exit_cost'], 3.)
        self.assertAlmostEqual(row['net_edge'], -.2)

    def test_future_entry_fair_is_not_used_and_null_fee_not_zero(self):
        self.tracker.add_fair('100001.SH', NOW+1, .1,.0999,.1001,10000)
        self.tracker.add_fair('100001.SH', NOW+60000, .1,.0999,.1001,10000)
        self.trade(fee=None); self.tracker.refresh(NOW+61000)
        self.assertFalse(self.data['edge']['t']['complete'])
        self.assertEqual(self.tracker.statistics('100001.SH',NOW+61000)['complete_lots'], 0)

    def test_missing_samples_prevent_positive_edge_admission(self):
        self.trade(); self.tracker.refresh(NOW+61000)
        stats = self.tracker.statistics('100001.SH', NOW+61000)
        self.assertFalse(stats['eligible'])

    def test_late_known_markout_and_trade_fee_cannot_leak_into_past(self):
        self.tracker.add_fair('100001.SH',NOW,.1002,.1001,.1003,10000)
        self.tracker.add_fair('100001.SH',NOW+62000,.1002,.1001,.1003,10000)
        self.trade()
        self.data['trades']['t']['observed_ms'] = NOW+63000
        self.tracker.refresh(NOW+61000)
        self.assertNotIn('t',self.data['edge'])
        self.tracker.refresh(NOW+63000)
        self.assertTrue(self.data['edge']['t']['complete'])
        self.assertEqual(self.tracker.statistics('100001.SH',NOW+61000)['complete_lots'],0)

    def test_trade_with_no_timestamp_is_not_silently_dropped_from_admission(self):
        IntegrationTests().seed_positive_edge(self.data,NOW)
        self.assertTrue(self.tracker.statistics('100001.SH',NOW)['eligible'])
        self.data['attempts']['missing'] = dict(order_id='missing',trade_date=20261102,status='filled',
                                               product_id='100001.SH',cum_qty=1)
        self.data['trades']['missing'] = dict(order_id='missing',trade_date=20261102,product_id='100001.SH',
                                             transact_time=None,last_qty=1,liquidity='MAKER',fee=-1.8)
        self.tracker.refresh(NOW)
        self.assertFalse(self.tracker.statistics('100001.SH',NOW)['eligible'])


class CoverageTests(unittest.TestCase):
    def test_hysteresis_and_no_future_evidence(self):
        policy = CoveragePolicy(max_symbols=1, min_hold_ms=10000, switch_ratio=1.2,
                                min_scale=.25, scale_step=.25, adjust_ms=1000)
        control = CoverageController(policy)
        def stat(score, stamp=NOW):
            return dict(eligible=True,score=score,as_of_ms=stamp)
        self.assertEqual(control.select({'a':stat(1)},NOW), ['a'])
        self.assertEqual(control.select({'a':stat(1),'b':stat(3)},NOW+1000), ['a'])
        self.assertEqual(control.select({'a':stat(1),'b':stat(3)},NOW+11000), ['b'])
        self.assertEqual(control.select({'future':stat(99,NOW+99999)},NOW+12000), [])

    def test_scale_never_exceeds_hard_cap_and_contracts_under_stress(self):
        control = CoverageController(CoveragePolicy(1,0,1.2,.25,.25,1000))
        for n in range(10): control.adjust(n*1001, utilization=.1, evidence_positive=True)
        self.assertLessEqual(control.scale,1.)
        previous=control.scale
        control.adjust(12000,utilization=.95,evidence_positive=True)
        self.assertLess(control.scale,previous)


def admission_data():
    return dict(account_id=1,attempts={
        'a':dict(probe='P2',order_id='p2',trade_date=20261102,market_crossed_ms=NOW-1000),
        'b':dict(probe='P3',order_id='p3',trade_date=20261102)}, trades={
        'a':dict(order_id='p2',trade_date=20261102,transact_time=NOW-500,fee=-1.8,liquidity='MAKER',last_qty=1),
        'b':dict(order_id='p3',trade_date=20261102,transact_time=NOW-500,fee=2.,liquidity='TAKER',last_qty=1)})


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = ProbeState(root/'state.json',1)
        self.addCleanup(self.state.close)
        self.journal = Journal(root/'events.jsonl')
        self.addCleanup(self.journal.close)
        helper = pricing_tests.PricingTests()
        self.engine = helper.engine(); helper.seed(self.engine)
        self.point = self.engine.value('100001.SH',NOW)
        self.portfolio = Portfolio()
        self.portfolio.account = dict(net=5000000,available=100000,openFrozen=False)
        self.portfolio.day_start_net = 5000000
        from mmbot.marketdata import MarketData
        self.market = MarketData()
        self.market.books['100001.SH'] = replace(book('100001.SH',self.point.price),recv_monotonic=time.monotonic())
        self.sdk = StrategyOrderManager(logging.getLogger('strategy-test'),'https://example.invalid/gateway')
        self.sdk.token = 'ak_'+'x'*43
        self.sdk.api_trading_enabled = True
        self.sdk.configure(SafetyGate(Limits.from_me(me()),{'100001.SH':product()}),self.portfolio,self.market,
                           self.state,self.journal,20261102,5000,
                           now=lambda:datetime.fromtimestamp(NOW/1000,ZoneInfo('Asia/Shanghai')),connected=lambda:True)
        confirmation = {k:'测试证据' for k in ('valuation','quote_rule','recovery_test','risk_free_rate')}
        self.admission = Admission(admission_data(),1,confirmation,NOW)
        self.hard = RiskLimits(15000,10000,5,100000,1000000,1000000,100000)
        self.sdk.attach_strategy(self.engine,self.hard,self.admission,lambda:True,lambda s,t:True,3)
        self.sdk.ledger_dirty = False
        self.quotes = make_quotes(self.point,self.market.books['100001.SH'],product(),QuotePolicy(1,2,3000))
        self.transport = patch('simtrade.service.order_manager.RestTools.post_data',return_value=Response(22,{'orderId':'a'}))
        self.post = self.transport.start(); self.addCleanup(self.transport.stop)

    def test_admission_does_not_accept_missing_or_future_probe_evidence(self):
        confirmation = {k:'测试' for k in ('valuation','quote_rule','recovery_test','risk_free_rate')}
        with self.assertRaises(ValueError): Admission({},1,confirmation,NOW)
        with self.assertRaises(ValueError): Admission(admission_data(),1,confirmation,NOW-2000)

    def test_unknown_first_leg_prevents_second_and_duplicate(self):
        results = self.sdk.submit_batch(self.quotes)
        self.assertEqual(len(results),1)
        self.assertEqual(self.post.call_count,1)
        with self.assertRaises(ProbeBlocked): self.sdk.submit_batch(self.quotes)
        self.assertEqual(self.post.call_count,1)

    def test_independent_risk_rejects_even_if_caller_says_quote_valid(self):
        self.sdk.soft_limits = replace(self.hard,max_loss=0)
        with self.assertRaises(ProbeBlocked): self.sdk.submit_batch(self.quotes)
        self.post.assert_not_called()

    def test_filled_response_requires_position_refresh_before_other_leg(self):
        def fill(url,payload,*args,**kwargs):
            raw = dict(payload,orderId='fill',ordStatus='2',cumQty=1,leavesQty=0)
            raw.pop('token',None)
            return Response(0,raw)
        self.post.side_effect = fill
        self.assertEqual(len(self.sdk.submit_batch(self.quotes)),1)
        self.assertTrue(self.sdk.ledger_dirty)
        with self.assertRaises(ProbeBlocked): self.sdk.submit_batch([self.quotes[1]])

    def test_reducing_label_cannot_create_a_short(self):
        with self.assertRaises(ProbeBlocked):
            self.sdk.submit_batch([Intent('100001.SH','2',self.market.books['100001.SH'].bid_px,1)],reducing=True)
        self.post.assert_not_called()

    def test_daily_bandwidth_whitelist_and_cutoff_are_not_bypassed(self):
        self.sdk.gate.daily_orders_used = self.sdk.gate.limits.daily_cap
        with self.assertRaises(ProbeBlocked): self.sdk.submit_batch(self.quotes)
        self.post.assert_not_called()

    def test_unknown_strategy_retry_still_uses_independent_risk(self):
        from mmbot.execution import QueryBudget
        from datetime import timedelta
        self.sdk.budget = QueryBudget(Limits.from_me(me()))
        self.sdk.submit_batch(self.quotes)
        attempt = next(iter(self.state.data['attempts'].values()))
        attempt['order_id'] = None
        self.sdk.now = lambda:datetime.fromtimestamp((NOW+6000)/1000,ZoneInfo('Asia/Shanghai'))
        self.engine.update(book('510050.SH',3,NOW+6000),NOW+6000)
        self.market.books['100001.SH'] = replace(self.market.books['100001.SH'],timestamp_ms=NOW+6000,recv_monotonic=time.monotonic())
        self.sdk.soft_limits = replace(self.hard,max_loss=0)
        self.post.reset_mock()
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)):
            self.sdk.expire()
        self.post.assert_not_called()
        self.assertEqual(attempt['status'],'unknown')

    def test_strategy_can_recover_with_same_ref_and_same_payload(self):
        from mmbot.execution import QueryBudget
        self.sdk.budget = QueryBudget(Limits.from_me(me()))
        self.post.return_value = Response(22,{})
        self.sdk.submit_batch(self.quotes)
        original = dict(self.post.call_args.args[1])
        self.sdk.now = lambda:datetime.fromtimestamp((NOW+6000)/1000,ZoneInfo('Asia/Shanghai'))
        self.engine.update(book('510050.SH',3,NOW+6000),NOW+6000)
        self.market.books['100001.SH'] = replace(self.market.books['100001.SH'],timestamp_ms=NOW+6000,recv_monotonic=time.monotonic())
        raw = dict(original,orderId='recovered',ordStatus='0',cumQty=0,leavesQty=1,tradeDate=20261102)
        raw.pop('token',None)
        self.post.return_value = Response(24,raw)
        self.post.reset_mock()
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)):
            self.sdk.expire()
        self.assertEqual(self.post.call_count,1)
        self.assertEqual(self.post.call_args.args[1],original)
        self.assertEqual(next(iter(self.state.data['attempts'].values()))['status'],'working')

    def test_stage_four_after_1455_only_verified_inventory_reduction(self):
        from mmbot.pricing import bs
        from mmbot.apifacts import round_to_tick
        close = datetime(2026,11,2,14,55,tzinfo=ZoneInfo('Asia/Shanghai'))
        stamp = int(close.timestamp()*1000)
        self.sdk.now = lambda: close
        self.sdk.stage = 4
        self.engine.update(book('510050.SH',3,stamp),stamp)
        mid = bs(3,3,self.engine.years('100001.SH',stamp),.02,0,.25,'C').price
        bid = round_to_tick(mid-.0001,.0001,'1')
        ask = round_to_tick(mid+.0001,.0001,'2')
        fresh = replace(book('100001.SH',mid,stamp),bid_px=bid,ask_px=ask,recv_monotonic=time.monotonic())
        self.engine.update(fresh,stamp)
        self.market.books['100001.SH'] = fresh
        with self.assertRaises(ProbeBlocked):
            self.sdk.submit_batch([Intent('100001.SH','1',bid,1)])
        self.portfolio.positions['100001.SH'] = dict(securityId='100001.SH',longQty=1,shortQty=0,sellableQty=1)
        result = self.sdk.submit_batch([Intent('100001.SH','2',bid,1)],reducing=True)
        self.assertEqual(len(result),1)
        self.assertEqual(self.post.call_args.args[1]['side'],'2')


class IntegrationTests(unittest.TestCase):
    def test_pm_acceptance_excludes_required_closing_window(self):
        from mmbot.strategy import StrategySession
        from types import SimpleNamespace
        end = datetime(2026,11,2,15,tzinfo=ZoneInfo('Asia/Shanghai'))
        end_ms = int(end.timestamp()*1000)
        start_ms = end_ms-7200000
        session = StrategySession.__new__(StrategySession)
        session.settings = SimpleNamespace(stage=3,symbols=('100001.SH',))
        session.now = lambda:end
        session.date = 20261102
        session.start_ms = start_ms
        session.failures = 0
        session.halting = False
        session.sdk = SimpleNamespace(rate_limit_count=0)
        session.monitor = SimpleNamespace(engine=SimpleNamespace(config=SimpleNamespace(max_book_age_ms=5000)))
        session.recorder = SimpleNamespace(market=SimpleNamespace(books={}))
        session.state = SimpleNamespace(data=dict(attempts={},stage3_sessions=[]),save=lambda:None)
        session.periods = {'20261102:PM':dict(first_quote_ms=start_ms,failures_start=0,rates_start=0,
                            covered_ms=117*60000,last_observation_ms=end_ms)}
        session.note_session(end_ms)
        self.assertTrue(session.state.data['stage3_sessions'][0]['passed'])

    def seed_positive_edge(self, state, now_ms):
        tracker = EdgeTracker(state,2.)
        for block in range(5):
            stamp = now_ms-1800000+block*300000
            for i in range(4):
                key = f'{block}-{i}'
                state['attempts'][key] = dict(order_id=key,trade_date=20261102,status='filled',
                    client_ref='old_'+key,product_id='100001.SH',side='1',price=.1,qty=1,
                    probe='MM',cum_qty=1,sent_ms=stamp+i,sent_monotonic=0,premium=1000,cancel_required=False)
                state['trades'][key] = dict(exec_id=key,order_id=key,trade_date=20261102,
                    product_id='100001.SH',side='1',last_qty=1,last_px=.1,fee=-1.8,liquidity='MAKER',transact_time=stamp+i)
                tracker.add_fair('100001.SH',stamp+i,.102,.1019,.1021,10000)
            for i in range(4):
                tracker.add_fair('100001.SH',stamp+60000+i,.102,.1019,.1021,10000)
                tracker.refresh(stamp+60000+i)
        self.assertTrue(tracker.statistics('100001.SH',now_ms)['eligible'])

    def test_real_sdk_stage_three_and_four_quotes_then_only_own_cancels(self):
        from mmbot.main import run
        from mmbot.config import Config
        from mmbot.pricing import PricingConfig
        from mmbot.strategy import StrategySettings
        from simtrade import MarketDataWebSocket,OrderWebSocket
        from simtrade.utils.rest_tools import RestTools
        helper = pricing_tests.PricingTests()
        engine = helper.engine(); helper.seed(engine)
        fair = engine.value('100001.SH',NOW).price
        for stage in (3,4):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                probe_path = root/'probe.json'
                probe_path.write_text(json.dumps(admission_data()))
                if stage == 4:
                    state = ProbeState(root/'strategy-state-1.json',1)
                    self.seed_positive_edge(state.data,NOW)
                    state.data['stage3_sessions'] = [dict(passed=True,symbol='100001.SH',end_ms=NOW-3600000)]
                    state.save(); state.close()
                settings = StrategySettings(stage,('100001.SH',),QuotePolicy(1,2,3000),
                    CoveragePolicy(1,300000,1.2,.25,.25,60000),probe_path,
                    {k:'离线模拟证据' for k in ('valuation','quote_rule','recovery_test','risk_free_rate')}, {})
                sockets, orders, calls = {}, {}, []
                def get(url,*args,**kwargs):
                    path = url.split('/gateway')[-1]; calls.append(path)
                    if path == '/me':
                        return Response(data=dict(accountId=1,tradeDate=20261102,serverTime=NOW,authType='API_KEY',
                            apiTradingEnabled=True,throttlePerSecond=10,dailyOrderLimit=100,dailyOrdersUsed=0,
                            httpPerSecond=100,httpPerMinute=1000,heavyPerMinute=120,wsConnections=0,wsMaxConnections=10))
                    if path == '/account': return Response(data=dict(net=5000000,available=100000,openFrozen=False))
                    if path == '/positions': return Response(data=[])
                    return Response(data=dict(items=[],page=0,size=500,hasMore=False))
                def post(url,payload,*args,**kwargs):
                    path = url.split('/gateway')[-1]; calls.append(path)
                    if path.startswith('/queryRefDataByUnderlying/'):
                        underlying = path.rsplit('/',1)[1]
                        return Response(data=[product(productId='100001.SH' if underlying=='510050.SH' else underlying+'opt',underlying=underlying)])
                    if path.startswith('/queryRefData/'):
                        return Response(data=dict(productId=path.rsplit('/',1)[1],securityType='FUT',tickSize=.2))
                    if path == '/addOrder':
                        order_id = 'new'+str(len(orders))
                        raw = dict(payload,orderId=order_id,tradeDate=20261102,ordStatus='0',cumQty=0,leavesQty=1)
                        raw.pop('token',None); orders[order_id] = raw
                        sockets['order'].on_data(dict(code=0,channel='trans.order',op='publish',data=[raw]))
                        return Response(data=raw)
                    if path == '/cancelOrderAsync':
                        order_id = payload['orderId']
                        self.assertIn(order_id,orders)
                        raw = dict(orders[order_id],ordStatus='4',leavesQty=0)
                        sockets['order'].on_data(dict(code=0,channel='trans.order',op='publish',data=[raw]))
                        return Response()
                    raise AssertionError(path)
                def start_order(ws):
                    sockets['order'] = ws; ws.started = ws.logged_in = True; ws.on_logged_in()
                    ws.on_data(dict(code=0,channel='trans.order',op='recover',data=[]))
                def start_md(ws):
                    ws.started = ws.logged_in = True
                    for symbol,mid in [('510050.SH',3),('100001.SH',fair)]:
                        ws.on_data(dict(code=0,channel='market',op='snapshot',data=dict(
                            productId=symbol,bidPx=mid-.00001,askPx=mid+.00001,bidSize=10,askSize=10,timestamp=NOW)))
                config = Config('https://example.invalid','ak_'+'x'*43,
                    ('IF2612.CFE','IH2612.CFE','IM2612.CFE','IC2612.CFE'),root,5000000)
                hard = RiskLimits(15000,10000,5,100000,1000000,1000000,100000)
                with patch.object(RestTools,'get_data',side_effect=get),patch.object(RestTools,'post_data',side_effect=post), \
                     patch.object(OrderWebSocket,'start',start_order),patch.object(MarketDataWebSocket,'start',start_md):
                    run(config,.3,risk_config=(PricingConfig(.02,0),hard,dict(atol=.001,rtol=.05)),strategy_settings=settings)
                self.assertEqual(calls.count('/addOrder'),2)
                self.assertEqual(calls.count('/cancelOrderAsync'),2)
                self.assertEqual(calls.count('/orders'),1)
                self.assertEqual({o['side'] for o in orders.values()},{'1','2'})
                final = json.loads((root/'strategy-state-1.json').read_text())
                self.assertTrue(all(a['status'] in TERMINAL for a in final['attempts'].values()))
                self.assertTrue((root/'strategy_report.md').exists())

if __name__=='__main__': unittest.main()
