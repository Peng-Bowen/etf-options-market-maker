import math
import unittest
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from test_stage1 import product
from mmbot.marketdata import Book
from mmbot.pricing import bs, implied_volatility, PricingEngine, PricingConfig, PricingError, compare_system_greeks
from mmbot.risk import RiskChecker, RiskLimits, Scenario, default_scenarios


NOW = int(datetime(2026,11,2,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)


def book(symbol, mid, stamp=NOW):
    return Book(symbol, mid-.00001, 10, mid+.00001, 10, mid, stamp, 0.)


class PricingTests(unittest.TestCase):
    def test_known_bs_price_parity_and_iv_roundtrip(self):
        call = bs(100,100,1,.05,0,.2,'C')
        put = bs(100,100,1,.05,0,.2,'P')
        self.assertAlmostEqual(call.price,10.450583572,places=8)
        self.assertAlmostEqual(call.price-put.price,100-100*math.exp(-.05),places=10)
        self.assertAlmostEqual(implied_volatility(call.price,100,100,1,.05,0,'C'),.2,places=7)

    def test_greeks_match_finite_differences(self):
        args = dict(spot=4,strike=4.1,years=.4,rate=.02,dividend=.01,vol=.3,kind='P')
        value = bs(**args)
        h = .0001
        up, down = bs(**dict(args,spot=4+h)),bs(**dict(args,spot=4-h))
        self.assertAlmostEqual(value.delta,(up.price-down.price)/(2*h),places=7)
        self.assertAlmostEqual(value.gamma,(up.price-2*value.price+down.price)/(h*h),places=6)
        for field,param,sign in [('vega','vol',1),('rho','rate',1),('theta','years',-1)]:
            diff=(bs(**dict(args,**{param:args[param]+h})).price-bs(**dict(args,**{param:args[param]-h})).price)/(2*h)
            self.assertAlmostEqual(getattr(value,field),sign*diff,places=6)

    def test_expiry_and_invalid_iv_are_explicit(self):
        self.assertEqual(bs(4,3,0,.02,0,.2,'C').price,1)
        for price in (-1,5,0):
            with self.assertRaises(PricingError): implied_volatility(price,4,4,1,0,0,'C')
        with self.assertRaises(PricingError): bs(float('nan'),4,1,0,0,.2,'C')

    def engine(self):
        return PricingEngine({'100001.SH':product()}, PricingConfig(rate=.02, dividend=0, iv_update_ms=60000, max_book_age_ms=5000, max_iv_age_ms=300000))

    def seed(self, engine):
        years=engine.years('100001.SH',NOW)
        mid=bs(3,3,years,.02,0,.25,'C').price
        engine.update(book('510050.SH',3),NOW)
        engine.update(book('100001.SH',mid),NOW)

    def test_iv_is_slow_but_underlying_moves_fair_immediately(self):
        engine=self.engine(); self.seed(engine)
        first=engine.value('100001.SH',NOW)
        engine.update(book('100001.SH',first.price*1.2,NOW+1000),NOW+1000)
        self.assertEqual(engine.value('100001.SH',NOW+1000).vol,first.vol)
        engine.update(book('510050.SH',3.03,NOW+1000),NOW+1000)
        self.assertGreater(engine.value('100001.SH',NOW+1000).price,first.price)

    def test_future_data_cannot_affect_current_or_retroactive_value(self):
        engine=self.engine(); self.seed(engine)
        old=engine.value('100001.SH',NOW)
        with self.assertRaises(PricingError): engine.update(book('510050.SH',9,NOW+10000),NOW)
        self.assertEqual(engine.value('100001.SH',NOW).price,old.price)
        engine.update(book('510050.SH',3.01,NOW+1000),NOW+1000)
        with self.assertRaises(PricingError): engine.value('100001.SH',NOW)

    def test_stale_and_expired_inputs_block(self):
        engine=self.engine(); self.seed(engine)
        with self.assertRaises(PricingError): engine.value('100001.SH',NOW+6000)
        with self.assertRaises(PricingError): engine.value('100001.SH',NOW+60*86400000)

    def test_system_comparison_preserves_null_and_sentinel(self):
        point=bs(3,3,.1,.02,0,.2,'C')
        raw={name:getattr(point,name) for name in ('delta','gamma','theta','vega','rho')}
        raw['volatility']=.2
        self.assertEqual(compare_system_greeks(point,raw,atol=.0001,rtol=.01),[])
        raw['vega']=None
        self.assertIn('vega:missing',compare_system_greeks(point,raw,atol=.0001,rtol=.01))
        raw['volatility']=.0001
        self.assertIn('volatility:unresolved',compare_system_greeks(point,raw,atol=.0001,rtol=.01))


class RiskTests(unittest.TestCase):
    def setUp(self):
        helper=PricingTests(); self.engine=helper.engine(); helper.seed(self.engine)
        self.point=self.engine.value('100001.SH',NOW)
        self.limits=RiskLimits(max_loss=15000,max_new_order_loss=10000,max_short_lots=5,
                               max_cash_delta_per_underlying=100000,max_cash_gamma=1000000,max_vega=1000000,
                               available_cash=100000)
        self.risk=RiskChecker({'100001.SH':self.point},self.limits,as_of_ms=NOW)

    def order(self,side='1',qty=1,price=None,**kw):
        return dict(productId='100001.SH',side=side,orderQty=qty,leavesQty=qty,
                    price=self.point.price if price is None else price,ordStatus='0',**kw)

    def test_loss_uses_full_repricing_and_adverse_orders_only(self):
        scenario=Scenario('drop',-.05,.10)
        risk=RiskChecker({'100001.SH':self.point},self.limits,NOW,[scenario])
        p=self.point
        new=bs(p.spot*.95,p.strike,p.years,p.rate,p.dividend,p.vol+.1,p.kind).price
        result=risk.evaluate([],[],[self.order(price=p.price+.1)])
        self.assertAlmostEqual(result.losses['drop'],max(0,(p.price+.1-new)*p.multiplier),places=8)

    def test_profitable_orders_never_offset_losing_order_risk(self):
        a=self.risk.evaluate([],[],[self.order('1',price=self.point.price+.1)])
        b=self.risk.evaluate([],[],[self.order('1',price=self.point.price+.1),self.order('2',price=self.point.price+.1)])
        self.assertGreaterEqual(b.worst_loss,a.worst_loss)

    def test_existing_positions_resting_and_candidates_are_joint(self):
        pos=[dict(securityId='100001.SH',longQty=0,shortQty=2)]
        result=self.risk.evaluate(pos,[self.order('2',qty=2)],[self.order('2',qty=2)])
        self.assertEqual(result.short_lots,6)
        ok,reasons=self.risk.check_hard_limits(pos,[self.order('2',qty=2)],[self.order('2',qty=2)])
        self.assertFalse(ok); self.assertIn('short_lots',reasons)

    def test_unknown_order_counts_and_final_order_ignored(self):
        pending=self.order(); pending['ordStatus']='U'
        final=dict(pending,ordStatus='4')
        self.assertGreater(self.risk.evaluate([],[pending],[]).worst_loss,0)
        self.assertEqual(self.risk.evaluate([],[final],[]).worst_loss,0)

    def test_inconsistent_remaining_quantity_cannot_hide_pending_risk(self):
        bad=dict(self.order(),ordStatus='U',leavesQty=0,cumQty=0)
        self.assertFalse(self.risk.check_hard_limits([],[bad],[])[0])
        partial=dict(self.order(qty=3),ordStatus='1',leavesQty=1,cumQty=2)
        self.assertEqual(self.risk.evaluate([],[partial],[]),self.risk.evaluate([],[self.order()],[]))

    def test_each_budget_is_independently_enforced(self):
        for field in ('max_loss','max_new_order_loss','max_short_lots','max_cash_delta_per_underlying','max_cash_gamma','max_vega','available_cash'):
            limits=replace(self.limits,**{field:0})
            risk=RiskChecker({'100001.SH':self.point},limits,NOW)
            self.assertFalse(risk.check_hard_limits([],[],[self.order('2')])[0],field)

    def test_missing_future_or_duplicate_inputs_fail_closed(self):
        self.assertFalse(self.risk.check_hard_limits([],[],[dict(self.order(),productId='UNKNOWN')])[0])
        future=replace(self.point,as_of_ms=NOW+1)
        with self.assertRaises(PricingError): RiskChecker({'100001.SH':future},self.limits,NOW)
        resting=self.order(orderId='a')
        self.assertFalse(self.risk.check_hard_limits([],[resting,resting],[])[0])

    def test_scenarios_include_isolated_underlying_shocks(self):
        scenarios=default_scenarios()
        self.assertTrue(any(s.underlying=='510050.SH' for s in scenarios))
        self.assertTrue(any(s.underlying_move==-.05 and s.vol_shift==.1 for s in scenarios))

    def test_nan_and_forged_cached_fair_value_cannot_bypass_risk(self):
        with self.assertRaises(PricingError):
            RiskChecker({'100001.SH':replace(self.point,vol=float('nan'))},self.limits,NOW)
        forged=replace(self.point,greeks=replace(self.point.greeks,price=999))
        risk=RiskChecker({'100001.SH':forged},self.limits,NOW)
        pos=[dict(securityId='100001.SH',longQty=1,shortQty=0)]
        self.assertEqual(risk.evaluate(pos,[],[]),self.risk.evaluate(pos,[],[]))

    def test_relabeling_stale_valuation_as_current_is_rejected(self):
        with self.assertRaises(PricingError):
            RiskChecker({'100001.SH':replace(self.point,underlying_ms=NOW-6000)},self.limits,NOW)
        with self.assertRaises(PricingError):
            RiskChecker({'100001.SH':replace(self.point,iv_as_of_ms=NOW-400000)},self.limits,NOW)

    def test_monitor_uses_sdk_positions_for_greek_comparison_and_logs_risk(self):
        import tempfile
        from pathlib import Path
        from mmbot.main import Recorder
        from mmbot.refdata import Universe
        from mmbot.journal import Journal,replay
        from mmbot.valuation_monitor import ValuationMonitor
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            with Journal(path) as journal:
                recorder=Recorder(journal,Universe({'100001.SH':product()},{}))
                recorder.portfolio.account=dict(available=100000)
                recorder.portfolio.positions['100001.SH']=dict(securityId='100001.SH',longQty=1,shortQty=0,
                    volatility=self.point.vol,**{k:getattr(self.point.greeks,k) for k in ('delta','gamma','theta','vega','rho')})
                monitor=ValuationMonitor(recorder,PricingConfig(rate=.02,dividend=0),self.limits,
                                         greek_atol=.001,greek_rtol=.01)
                for b in (book('510050.SH',3),book('100001.SH',self.point.price)):
                    raw=dict(productId=b.product_id,bidPx=b.bid_px,askPx=b.ask_px,bidSize=1,askSize=1,timestamp=NOW)
                    monitor.on_event('market',raw,'bbo',0,NOW)
                monitor.engine.value('100001.SH',NOW)
                result=monitor.snapshot(NOW)
                self.assertTrue(result['passed'])
                self.assertEqual(result['system_greek_warnings']['100001.SH'],[])
            self.assertIn('valuation_risk',[r['kind'] for r in replay(path)])


if __name__=='__main__': unittest.main()
