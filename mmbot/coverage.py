"""多合约覆盖、迟滞与分级预算；不把未测成交概率写成参数。"""
from dataclasses import dataclass, replace
import math


@dataclass(frozen=True)
class CoveragePolicy:
    max_symbols: int
    min_hold_ms: int
    switch_ratio: float
    min_scale: float
    scale_step: float
    adjust_ms: int
    relax_below: float = .5
    tighten_above: float = .8

    def __post_init__(self):
        if (type(self.max_symbols) is not int or not 1 <= self.max_symbols <= 9
                or type(self.min_hold_ms) is not int or self.min_hold_ms < 0
                or not math.isfinite(self.switch_ratio) or self.switch_ratio <= 1
                or not 0 < self.min_scale <= 1 or not 0 < self.scale_step <= 1
                or type(self.adjust_ms) is not int or self.adjust_ms <= 0
                or not 0 <= self.relax_below < self.tighten_above <= 1):
            raise ValueError('无效覆盖或动态预算参数')


class CoverageController:
    def __init__(self, policy):
        self.policy = policy
        self.active = []
        self.since = {}
        self.scale = policy.min_scale
        self.last_adjust = -policy.adjust_ms

    def select(self, evidence, now_ms, stranded_cost=None):
        stranded_cost = stranded_cost or {}
        valid = {s:r for s,r in evidence.items() if r['eligible'] and r['as_of_ms'] <= now_ms}
        selected = [s for s in self.active if s in valid]
        ranked = sorted(valid, key=lambda s: valid[s]['score'], reverse=True)
        for symbol in ranked:
            if symbol in selected:
                continue
            if len(selected) < self.policy.max_symbols:
                selected.append(symbol); self.since[symbol] = now_ms
                continue
            movable = [s for s in selected if now_ms-self.since[s] >= self.policy.min_hold_ms]
            if not movable:
                break
            weakest = min(movable, key=lambda s:valid[s]['score'])
            # 以至少一个驻留期的可观测收入比较一次退出摩擦，量纲均为元。
            horizon = max(1., self.policy.min_hold_ms/1000)
            benefit = (valid[symbol]['score']-valid[weakest]['score']*self.policy.switch_ratio)*horizon
            if benefit > stranded_cost.get(weakest,0.):
                selected.remove(weakest); selected.append(symbol); self.since[symbol] = now_ms
        self.active = selected
        return list(selected)

    def adjust(self, now_ms, utilization, evidence_positive):
        if now_ms-self.last_adjust < self.policy.adjust_ms:
            return self.scale
        if not math.isfinite(utilization) or utilization >= self.policy.tighten_above or not evidence_positive:
            self.scale = max(self.policy.min_scale, self.scale-self.policy.scale_step)
        elif utilization < self.policy.relax_below:
            self.scale = min(1., self.scale+self.policy.scale_step)
        self.last_adjust = now_ms
        return self.scale

    def limits(self, hard_limits):
        return replace(hard_limits, max_loss=hard_limits.max_loss*self.scale,
                       max_new_order_loss=hard_limits.max_new_order_loss*self.scale,
                       max_short_lots=math.floor(hard_limits.max_short_lots*self.scale),
                       max_cash_delta_per_underlying=hard_limits.max_cash_delta_per_underlying*self.scale,
                       max_cash_gamma=hard_limits.max_cash_gamma*self.scale,
                       max_vega=hard_limits.max_vega*self.scale)
