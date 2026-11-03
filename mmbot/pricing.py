"""欧式 ETF 期权 BS 定价。希腊值按每份标的、每年、IV 变化 1.00。

采用显式 spot/r/q 口径；对应远期 F=S*exp((r-q)T)。没有经验证的
指数期货→ETF 映射时只用 ETF BBO，禁止直接以指数点位替代 ETF 价格。
"""
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .apifacts import assert_allowed


class PricingError(ValueError):
    pass


def finite(*values):
    if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in values):
        raise PricingError('输入必须是有限数值')


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def bs(spot, strike, years, rate, dividend, vol, kind) -> Greeks:
    finite(spot,strike,years,rate,dividend,vol)
    if spot<=0 or strike<=0 or years<0 or vol<0 or kind not in ('C','P'):
        raise PricingError('无效 BS 输入')
    sign=1 if kind=='C' else -1
    if years==0:
        delta=sign if sign*(spot-strike)>0 else (sign*.5 if spot==strike else 0.)
        return Greeks(max(sign*(spot-strike),0.),delta,0.,0.,0.,0.)
    discount=math.exp(-rate*years); carry=math.exp(-dividend*years)
    if vol==0:
        spread=spot*carry-strike*discount
        active=1. if sign*spread>0 else (.5 if spread==0 else 0.)
        return Greeks(max(sign*spread,0.),sign*carry*active,0.,
                      sign*(dividend*spot*carry-rate*strike*discount)*active,0.,sign*strike*years*discount*active)
    root=math.sqrt(years)
    d1=(math.log(spot/strike)+(rate-dividend+.5*vol*vol)*years)/(vol*root)
    d2=d1-vol*root
    cdf=lambda x: .5*math.erfc(-x/math.sqrt(2))
    density=math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)
    nd1,nd2=cdf(sign*d1),cdf(sign*d2)
    return Greeks(max(0.,sign*(spot*carry*nd1-strike*discount*nd2)),
                  sign*carry*nd1,carry*density/(spot*vol*root),
                  -spot*carry*density*vol/(2*root)-sign*rate*strike*discount*nd2+sign*dividend*spot*carry*nd1,
                  spot*carry*density*root,sign*strike*years*discount*nd2)


def implied_volatility(price, spot, strike, years, rate, dividend, kind, max_vol=5., tolerance=1e-10):
    finite(price,max_vol,tolerance)
    lower=bs(spot,strike,years,rate,dividend,0.,kind).price
    upper=spot*math.exp(-dividend*years) if kind=='C' else strike*math.exp(-rate*years)
    if years<=0 or max_vol<=0 or tolerance<=0 or not lower+tolerance<price<upper-tolerance:
        raise PricingError('价格在无套利边界外或 IV 不可识别')
    if bs(spot,strike,years,rate,dividend,max_vol,kind).price<price:
        raise PricingError('IV 超出求解上限，不采用边界值冒充解')
    lo,hi=0.,max_vol
    for _ in range(100):
        mid=(lo+hi)/2
        fitted=bs(spot,strike,years,rate,dividend,mid,kind).price
        if abs(fitted-price)<=tolerance:
            return mid
        if fitted<price: lo=mid
        else: hi=mid
    raise PricingError('IV 未收敛')


@dataclass(frozen=True)
class PricingConfig:
    rate: float
    dividend: float
    iv_update_ms: int = 60000
    max_book_age_ms: int = 5000
    max_iv_age_ms: int = 300000
    max_pair_skew_ms: int = 3000

    def __post_init__(self):
        finite(self.rate,self.dividend)
        if any(type(v) is not int or v<=0 for v in (self.iv_update_ms,self.max_book_age_ms,self.max_iv_age_ms,self.max_pair_skew_ms)):
            raise PricingError('定价时间阈值须为正整数毫秒')


@dataclass(frozen=True)
class Valuation:
    product_id: str
    underlying: str
    spot: float
    strike: float
    years: float
    rate: float
    dividend: float
    vol: float
    kind: str
    multiplier: float
    sell_margin: float
    as_of_ms: int
    iv_as_of_ms: int
    underlying_ms: int
    greeks: Greeks

    @property
    def price(self): return self.greeks.price


def compare_system_greeks(local: Greeks, position: dict, *, atol: float, rtol: float) -> list:
    """A26：使用查询的 snake_case dict，null 与最低 IV 哨兵均单独告警。
    应使用系统 volatility 重新计算 local 后比较，以区分模型口径和 IV 差异。
    """
    finite(atol,rtol)
    if atol<0 or rtol<0: raise PricingError('校验容差不能为负')
    issues=[]
    if position.get('volatility') is None: issues.append('volatility:missing')
    elif position['volatility']==.0001: issues.append('volatility:unresolved')
    for name in ('delta','gamma','theta','vega','rho'):
        remote=position.get(name)
        if remote is None:
            issues.append(name+':missing'); continue
        if not isinstance(remote,(float,int)) or not math.isfinite(remote):
            issues.append(name+':invalid'); continue
        if not math.isclose(getattr(local,name),remote,rel_tol=rtol,abs_tol=atol):
            issues.append(name+':mismatch')
    return issues


class PricingEngine:
    def __init__(self, products: dict, config: PricingConfig):
        self.products={k:dict(v) for k,v in products.items()}
        self.config=config
        self.books={}
        self.ivs={}
        self.clock_ms=0
        self.expiries={}
        for symbol,p in self.products.items():
            assert_allowed(p)
            if p.get('exerciseStyle','E')!='E': raise PricingError('仅支持欧式期权')
            finite(p['strikePx'],p['contractMultiplier'],p['sellMargin'])
            if p['strikePx']<=0 or p['contractMultiplier']<=0 or p['sellMargin']<0:
                raise PricingError('参考数据缺少有效行权价/乘数/保证金')
            expiry=datetime.strptime(str(p['maturityDate']),'%Y%m%d').replace(hour=15,tzinfo=ZoneInfo('Asia/Shanghai'))
            self.expiries[symbol]=int(expiry.timestamp()*1000)

    def years(self,symbol,now_ms):
        return max(0.,(self.expiries[symbol]-now_ms)/(365*86400000))

    def _fresh(self,book,now_ms):
        if not book.is_valid or not 0<=now_ms-book.timestamp_ms<=self.config.max_book_age_ms:
            raise PricingError('行情无效、陈旧或来自未来')

    def update(self,book,now_ms):
        if now_ms<self.clock_ms or book.timestamp_ms>now_ms:
            raise PricingError('事件时间倒退或引用未来快照')
        self.clock_ms=now_ms
        old=self.books.get(book.product_id)
        if old and book.timestamp_ms>0 and book.timestamp_ms<=old.timestamp_ms: return False
        self.books[book.product_id]=book
        if book.product_id not in self.products: return False
        symbol=book.product_id
        previous=self.ivs.get(symbol)
        if previous and now_ms-previous[1]<self.config.iv_update_ms: return False
        p=self.products[symbol]
        underlying=self.books.get(p['underlying'])
        if underlying is None: return False
        self._fresh(book,now_ms); self._fresh(underlying,now_ms)
        if abs(book.timestamp_ms-underlying.timestamp_ms)>self.config.max_pair_skew_ms:
            raise PricingError('标的和期权快照相差过大，不能拟合 IV')
        vol=implied_volatility((book.bid_px+book.ask_px)/2,(underlying.bid_px+underlying.ask_px)/2,
                              p['strikePx'],self.years(symbol,now_ms),self.config.rate,self.config.dividend,p['callOrPut'])
        # 只拟合当时已经接收且可见的一对快照，不回填之前的历史时点。
        self.ivs[symbol]=(vol,now_ms)
        return True

    def value(self,symbol,now_ms) -> Valuation:
        if now_ms<self.clock_ms: raise PricingError('引擎已前进，不能用当前状态回看历史')
        p=self.products[symbol]
        u=self.books.get(p['underlying'])
        if u is None or symbol not in self.ivs: raise PricingError('尚无标的行情或有效 IV')
        self._fresh(u,now_ms)
        vol,ivtime=self.ivs[symbol]
        if not 0<=now_ms-ivtime<=self.config.max_iv_age_ms: raise PricingError('IV 陈旧或来自未来')
        years=self.years(symbol,now_ms)
        if years<=0: raise PricingError('到期期权不能继续参与报价风控')
        spot=(u.bid_px+u.ask_px)/2
        greeks=bs(spot,p['strikePx'],years,self.config.rate,self.config.dividend,vol,p['callOrPut'])
        return Valuation(symbol,p['underlying'],spot,p['strikePx'],years,self.config.rate,self.config.dividend,
                         vol,p['callOrPut'],p['contractMultiplier'],p['sellMargin'],now_ms,ivtime,u.timestamp_ms,greeks)

    def check_system_position(self,position,now_ms,*,atol,rtol):
        point=self.value(position['security_id'],now_ms)
        vol=position.get('volatility')
        local=point.greeks
        if vol is not None and vol!=.0001:
            local=bs(point.spot,point.strike,point.years,point.rate,point.dividend,vol,point.kind)
        return compare_system_greeks(local,position,atol=atol,rtol=rtol)
