"""阶段三只读估值观察器。无下单出口，也不取代执行层最终风险复核。"""
import json
from dataclasses import asdict, replace
from pathlib import Path

from .marketdata import Book
from .pricing import PricingConfig, PricingEngine, PricingError, finite
from .risk import RiskChecker, RiskLimits


def load_risk_config(path: Path):
    return parse_risk_config(json.loads(path.read_text(encoding='utf-8')))


def parse_risk_config(data):
    config=PricingConfig(**data['pricing'])
    limits=RiskLimits(**data['limits'])
    comparison=data['greek_comparison']
    finite(comparison['atol'],comparison['rtol'])
    if min(comparison['atol'],comparison['rtol'])<0: raise PricingError('希腊值比较容差必须非负')
    return config,limits,comparison


class ValuationMonitor:
    def __init__(self,recorder,config,limits,*,greek_atol,greek_rtol):
        self.recorder=recorder
        self.engine=PricingEngine(recorder.universe.options,config)
        self.limits=limits
        self.atol,self.rtol=greek_atol,greek_rtol
        self.last_snapshot_ms=0
        self.errors={}
        recorder.listeners.append(self.on_event)

    def on_event(self,kind,raw,op,mono,wall):
        if kind!='market': return
        now_ms=wall+(self.recorder.journal.server_offset_ms or 0)
        b=Book(raw['productId'],float(raw.get('bidPx') or 0),int(raw.get('bidSize') or 0),
               float(raw.get('askPx') or 0),int(raw.get('askSize') or 0),float(raw.get('last') or 0),
               int(raw.get('timestamp') or 0),mono)
        try:
            self.engine.update(b,now_ms)
            self.errors.pop(b.product_id,None)
        except PricingError as exc:
            message=str(exc)
            if self.errors.get(b.product_id)!=message:
                self.recorder.journal.write('pricing_unavailable',dict(product_id=b.product_id,reason=message),b.timestamp_ms or None)
            self.errors[b.product_id]=message

    def snapshot(self,now_ms):
        values={}; unavailable={}; comparisons={}
        positions=list(self.recorder.portfolio.positions.values())
        orders=self.recorder.portfolio.active_orders
        required={p['securityId'] for p in positions if p.get('longQty',0) or p.get('shortQty',0)}
        required.update(o.get('productId') for o in orders)
        # 输出已能定价的合约，同时将所有未定价敞口作为硬失败，不能静默漏仓。
        for symbol in set(self.engine.ivs)|required:
            try:
                values[symbol]=self.engine.value(symbol,now_ms)
            except (PricingError,KeyError) as exc:
                unavailable[str(symbol)]=str(exc)
        for p in positions:
            symbol=p['securityId']
            if symbol not in values: continue
            system=dict(p,security_id=symbol)
            try:
                comparisons[symbol]=self.engine.check_system_position(system,now_ms,atol=self.atol,rtol=self.rtol)
            except PricingError as exc:
                comparisons[symbol]=[str(exc)]
        account_cash=self.recorder.portfolio.account.get('available')
        reasons=[]; result=None
        if any(symbol not in values for symbol in required): reasons.append('unpriced_exposure')
        if account_cash is None: reasons.append('available_cash_unknown')
        else:
            try:
                finite(account_cash)
                if account_cash<0: reasons.append('available_cash_negative')
                if not reasons:
                    limits=replace(self.limits,available_cash=min(account_cash,self.limits.available_cash))
                    checker=RiskChecker(values,limits,now_ms,max_underlying_age_ms=self.engine.config.max_book_age_ms,
                                        max_iv_age_ms=self.engine.config.max_iv_age_ms)
                    result=checker.evaluate(positions,orders,[])
                    _,reasons=checker.check_hard_limits(positions,orders,[])
            except (PricingError,KeyError,TypeError,OverflowError) as exc:
                reasons.append(f'invalid_input:{exc}')
        report=dict(as_of_ms=now_ms,passed=not reasons,reasons=reasons,
                    valuations={k:asdict(v) for k,v in values.items()},unavailable=unavailable,
                    system_greek_warnings=comparisons,stress=asdict(result) if result else None,
                    scope='existing_positions_and_resting_orders_only')
        self.recorder.journal.write('valuation_risk',report)
        self.last_snapshot_ms=now_ms
        return report
