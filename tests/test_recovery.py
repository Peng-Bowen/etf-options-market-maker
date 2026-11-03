import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_stage2 as fixtures
from mmbot.execution import QueryBudget
from mmbot.config import Limits
from mmbot.probe_execution import ProbeBlocked


class RecoveryTests(unittest.TestCase):
    # 复用账户/假传输夹具，避免任何恢复测试接触网络。
    def setUp(self):
        fixtures.ProbeTests.setUp(self)
        self.sdk.budget = QueryBudget(Limits.from_me(fixtures.me()))

    tearDown = fixtures.ProbeTests.tearDown
    submit = fixtures.ProbeTests.submit

    def unknown(self):
        with patch('simtrade.service.order_manager.RestTools.post_data',return_value=fixtures.Response(22,{})) as post:
            attempt = self.submit()
            self.original_payload = copy.deepcopy(post.call_args.args[1])
        return attempt

    def advance(self, seconds):
        self.sdk.now = lambda: fixtures.NOW+timedelta(seconds=seconds)
        stamp = int(self.sdk.now().timestamp()*1000)
        raw = dict(productId='100001.SH',bidPx=.01,askPx=.0102,bidSize=10,askSize=10,timestamp=stamp)
        self.md.update(raw)
        return stamp

    def test_retry_uses_exact_ref_and_payload(self):
        attempt = self.unknown()
        self.advance(6)
        returned = dict(self.original_payload,orderId='recovered',ordStatus='0',cumQty=0,leavesQty=1,tradeDate=20261102)
        returned.pop('token',None)
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data',return_value=fixtures.Response(24,returned)) as post:
            self.sdk.expire()
        self.assertEqual(post.call_args.args[1],self.original_payload)
        self.assertEqual(len(self.state.data['attempts']),1)
        self.assertEqual(attempt['status'],'working')
        self.assertEqual(attempt['order_id'],'recovered')

    def test_query_found_order_cancels_after_timeout_without_resubmission(self):
        attempt = self.unknown(); self.advance(121)
        raw = dict(orderId='found',clientRef=attempt['client_ref'],tradeDate=20261102,
                   productId='100001.SH',side='1',price=.0099,orderQty=1,cumQty=0,leavesQty=1,ordStatus='0')
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[dict(raw=raw)],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data',return_value=fixtures.Response()) as post:
            self.sdk.expire()
        self.assertTrue(all(c.args[0].endswith('/cancelOrderAsync') for c in post.call_args_list))
        self.assertTrue(attempt['cancel_required'])

    def test_retry_rejection_does_not_prove_original_was_rejected(self):
        attempt = self.unknown(); self.advance(6)
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data',return_value=fixtures.Response(12)):
            self.sdk.expire()
        self.assertEqual(attempt['status'],'unknown')

    def test_stop_and_next_day_never_resubmit_old_order(self):
        attempt = self.unknown(); self.advance(6)
        self.gate.stopped = True
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data') as post:
            self.sdk.expire(all_orders=True)
            self.advance(86406)
            self.sdk.expire()
        post.assert_not_called()
        self.assertTrue(attempt['cancel_required'])

    def test_complete_scans_expire_absence_but_late_push_revives_for_cancel(self):
        attempt = self.unknown()
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data') as post:
            self.advance(301); self.sdk.expire()
            self.assertEqual(attempt['status'],'unknown')
            self.advance(362); self.sdk.expire()
            self.assertEqual(attempt['status'],'not_found')
            post.assert_not_called()
        raw = dict(orderId='late',clientRef=attempt['client_ref'],tradeDate=20261102,ordStatus='0',cumQty=0,leavesQty=1)
        self.sdk.on_order(raw)
        self.assertEqual(attempt['status'],'working')
        self.assertTrue(attempt['cancel_required'])

    def test_failed_or_partial_scan_never_releases_risk(self):
        attempt = self.unknown()
        with patch.object(self.sdk,'query_orders',side_effect=[dict(items=[],has_more=True),None,None]):
            for seconds in (301,362,423):
                self.advance(seconds); self.sdk.expire()
        self.assertEqual(attempt['status'],'unknown')

    def test_no_unknown_orders_means_no_order_queries(self):
        with patch.object(self.sdk,'query_orders') as query:
            self.sdk.expire()
            query.assert_not_called()

    def test_retry_must_pass_current_safety_gate(self):
        attempt = self.unknown(); self.advance(6)
        self.portfolio.account['net'] = 4_975_000
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data') as post:
            self.sdk.expire()
        post.assert_not_called()
        self.assertEqual(attempt['status'],'unknown')

    def test_retry_is_bounded_and_does_not_reset_quote_deadline(self):
        attempt = self.unknown(); sent = attempt['sent_ms']
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data',return_value=fixtures.Response(22,{})) as post:
            for seconds in (6,12,18,24):
                self.advance(seconds); self.sdk.expire()
        self.assertEqual(post.call_count,2)
        self.assertEqual(attempt['sent_ms'],sent)

    def test_same_ref_retry_after_restart_is_not_allowed(self):
        attempt = self.unknown()
        self.sdk.configure(self.gate,self.portfolio,self.md,self.state,self.journal,20261102,500,
                           now=lambda: fixtures.NOW+timedelta(seconds=6),connected=lambda:True)
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)), \
             patch('simtrade.service.order_manager.RestTools.post_data') as post:
            self.sdk.expire()
        post.assert_not_called()
        self.assertTrue(attempt['cancel_required'])

    def test_sdk_local_x_does_not_reblock_a_reconciled_absence(self):
        attempt = self.unknown()
        attempt['order_id'] = 'missing'
        with patch.object(self.sdk,'query_orders',return_value=dict(items=[],has_more=False)):
            self.advance(301); self.sdk.expire()
            self.advance(362); self.sdk.expire()
        self.assertEqual(attempt['status'],'not_found')
        self.sdk.on_order(dict(orderId='missing',clientRef=attempt['client_ref'],tradeDate=20261102,
                               ordStatus='X',cumQty=0,leavesQty=0))
        self.assertEqual(attempt['status'],'not_found')
        self.assertEqual(self.portfolio.active_orders,[])


if __name__ == '__main__': unittest.main()
