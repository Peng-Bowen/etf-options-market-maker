"""独立硬约束检查：全额重定价、联合持仓与全部可能成交的挂单。

不依赖 quoter，不猜条件成交率。压力损失是给定情景下的模型上界，
不是所有未来行情/流动性/手续费的保证。空头与资金计量不假定组合保证金。
"""
from dataclasses import dataclass, replace
from itertools import product

from .apifacts import ALLOWED_UNDERLYINGS, FINAL_STATUSES
from .pricing import PricingError, bs, finite


@dataclass(frozen=True)
class Scenario:
    name: str
    underlying_move: float
    vol_shift: float
    underlying: str = None

    def __post_init__(self):
        finite(self.underlying_move,self.vol_shift)
        if self.underlying_move<=-1 or not self.name or (self.underlying is not None and self.underlying not in ALLOWED_UNDERLYINGS):
            raise PricingError('无效压力情景')


def default_scenarios():
    return [Scenario(f'{u or "ALL"}:{move:+.2f}:{vol:+.2f}',move,vol,u)
            for u,move,vol in product((None,)+ALLOWED_UNDERLYINGS,(-.05,-.02,0.,.02,.05),(-.05,0.,.05,.10))
            if move or vol]


@dataclass(frozen=True)
class RiskLimits:
    max_loss: float
    max_new_order_loss: float
    max_short_lots: int
    max_cash_delta_per_underlying: float
    max_cash_gamma: float
    max_vega: float
    available_cash: float

    def __post_init__(self):
        finite(*vars(self).values())
        if any(v<0 for v in vars(self).values()) or type(self.max_short_lots) is not int:
            raise PricingError('限额须非负，空头张数须整数；不提供隐含默认风险预算')


@dataclass(frozen=True)
class StressResult:
    losses: dict
    new_order_losses: dict
    short_lots: int
    cash_delta: float
    cash_gamma: float
    vega: float
    new_cash: float

    @property
    def worst_loss(self): return max(self.losses.values(),default=0.)


class RiskChecker:
    def __init__(self,valuations,limits:RiskLimits,as_of_ms:int,scenarios=None,
                 max_underlying_age_ms=5000,max_iv_age_ms=300000):
        self.values=dict(valuations)
        self.limits=limits
        self.as_of_ms=as_of_ms
        self.scenarios=tuple(default_scenarios() if scenarios is None else scenarios)
        if (not self.scenarios or len({s.name for s in self.scenarios})!=len(self.scenarios)
                or any(s.name=='__base__' for s in self.scenarios)):
            raise PricingError('情景集不能为空或重名')
        for symbol,p in self.values.items():
            finite(p.spot,p.strike,p.years,p.rate,p.dividend,p.vol,p.multiplier,p.sell_margin,
                   p.as_of_ms,p.iv_as_of_ms,p.underlying_ms)
            if p.product_id!=symbol or p.as_of_ms!=as_of_ms or p.iv_as_of_ms>as_of_ms or p.underlying_ms>as_of_ms:
                raise PricingError('估值必须为同一当前时点且不引用未来数据')
            if as_of_ms-p.underlying_ms>max_underlying_age_ms or as_of_ms-p.iv_as_of_ms>max_iv_age_ms:
                raise PricingError('估值引用的标的行情或 IV 已陈旧，不能只改评估标签')
            if p.underlying not in ALLOWED_UNDERLYINGS or p.years<=0 or p.multiplier<=0 or p.sell_margin<0:
                raise PricingError('合约不在允许范围内或已到期')
            self.values[symbol]=replace(p,greeks=bs(p.spot,p.strike,p.years,p.rate,p.dividend,p.vol,p.kind))

    def _point(self,symbol):
        if symbol not in self.values: raise PricingError(f'缺少 {symbol} 的有效估值')
        return self.values[symbol]

    @staticmethod
    def _qty(value):
        if type(value) is not int or value<0: raise PricingError('数量必须是非负整数张')
        return value

    def _orders(self,orders,seen):
        result=[]
        for o in orders:
            if o.get('ordStatus') in FINAL_STATUSES: continue
            key=o.get('orderId') or o.get('clientRef')
            if key and key in seen: raise PricingError('重复委托，必须先按委托号去重')
            if key: seen.add(key)
            p=self._point(o['productId'])
            if str(o['side']) not in ('1','2'): raise PricingError('无效方向')
            side=1 if str(o['side'])=='1' else -1
            if o.get('leavesQty') is not None:
                qty=self._qty(o['leavesQty'])
                if 'orderQty' in o and qty!=self._qty(o['orderQty'])-self._qty(o.get('cumQty',0)):
                    raise PricingError('未完成委托剩余量与累计成交矛盾，不能释放风险占用')
            else:
                qty=self._qty(o['orderQty'])-self._qty(o.get('cumQty',0))
                if qty<0: raise PricingError('累计成交超过委托数量')
            price=o['price']; finite(price)
            if price<=0: raise PricingError('压力检查仅接受明确成交上/下界的正价限价单')
            if o.get('ordType','2') not in ('2',2): raise PricingError('不支持无成交价格边界的市价单')
            if qty: result.append((p,side,qty,price))
        return result

    def evaluate(self,positions,resting_orders,candidate_orders) -> StressResult:
        held=[]; symbols=set(); shorts=0
        for row in positions:
            symbol=row['securityId']
            if symbol in symbols: raise PricingError('重复持仓行')
            symbols.add(symbol)
            long=self._qty(row['longQty']); short=self._qty(row['shortQty'])
            if long or short:
                held.append((self._point(symbol),long-short)); shorts+=short
        seen=set()
        resting=self._orders(resting_orders,seen)
        new=self._orders(candidate_orders,seen)
        orders=resting+new
        needed={p.product_id for p,_ in held}|{p.product_id for p,_,_,_ in orders}
        # 不假定卖单一定平多，也不假定买单能抵销未来卖单，故此张数上界保守。
        shorts+=sum(q for _,s,q,_ in orders if s<0)
        new_cash=sum(q*(price*p.multiplier if side>0 else p.sell_margin) for p,side,q,price in new)
        losses={}; new_losses={}; maxdelta=maxgamma=maxvega=0.
        # 含基准态：即使波动情景恰好抵消，也不能漏掉即时敞口。
        for scenario in (Scenario('__base__',0.,0.),)+self.scenarios:
            repriced={}
            for symbol in needed:
                p=self.values[symbol]
                affected=scenario.underlying is None or scenario.underlying==p.underlying
                move=scenario.underlying_move if affected else 0.
                vol=max(0.,p.vol+(scenario.vol_shift if affected else 0.))
                spot=p.spot*(1+move)
                repriced[symbol]=(bs(spot,p.strike,p.years,p.rate,p.dividend,vol,p.kind),spot)
            pnl=sum(q*(repriced[p.product_id][0].price-p.price)*p.multiplier for p,q in held)
            adverse=[min(0.,side*(repriced[p.product_id][0].price-price)*p.multiplier*q)
                     for p,side,q,price in orders]
            losses[scenario.name]=max(0.,-pnl-sum(adverse))
            new_losses[scenario.name]=-sum(min(0.,side*(repriced[p.product_id][0].price-price)*p.multiplier*q)
                                                   for p,side,q,price in new)
            # 挂单各自可成交/不成交：每个希腊指标求区间端点，避免假定双边同时填满。
            for metric in ('delta','gamma','vega'):
                groups={}
                def coefficient(p):
                    g,spot=repriced[p.product_id]
                    return getattr(g,metric)*p.multiplier*(spot if metric=='delta' else spot*spot if metric=='gamma' else 1.)
                for p,q in held:
                    key=p.underlying if metric=='delta' else 'ALL'
                    pair=groups.setdefault(key,[0.,0.]); value=q*coefficient(p)
                    pair[0]+=value; pair[1]+=value
                for p,side,q,_ in orders:
                    key=p.underlying if metric=='delta' else 'ALL'
                    pair=groups.setdefault(key,[0.,0.]); value=side*q*coefficient(p)
                    pair[0]+=min(0.,value); pair[1]+=max(0.,value)
                maximum=max((max(abs(lo),abs(hi)) for lo,hi in groups.values()),default=0.)
                if metric=='delta': maxdelta=max(maxdelta,maximum)
                elif metric=='gamma': maxgamma=max(maxgamma,maximum)
                else: maxvega=max(maxvega,maximum)
        return StressResult(losses,new_losses,shorts,maxdelta,maxgamma,maxvega,new_cash)

    def check_hard_limits(self,positions,resting_orders,candidate_orders):
        """每次重算，不接受评分器传入的通过标志或缓存风险分数。"""
        try:
            result=self.evaluate(positions,resting_orders,candidate_orders)
        except (PricingError,KeyError,TypeError,OverflowError) as exc:
            return False,[f'invalid_input:{exc}']
        tests=((result.worst_loss,self.limits.max_loss,'stress_loss'),
               (max(result.new_order_losses.values()),self.limits.max_new_order_loss,'new_order_loss'),
               (result.short_lots,self.limits.max_short_lots,'short_lots'),
               (result.cash_delta,self.limits.max_cash_delta_per_underlying,'cash_delta'),
               (result.cash_gamma,self.limits.max_cash_gamma,'cash_gamma'),
               (result.vega,self.limits.max_vega,'vega'),(result.new_cash,self.limits.available_cash,'cash'))
        reasons=[name for value,limit,name in tests if value>limit]
        return not reasons,reasons
