"""安全规则与只读 SDK。阶段 1 没有任何可用的交易出口。

SafetyGate 是后续执行层的必要条件，不代表已完成压力风控或获准交易。
不提供 enable_trading / force / skip_checks 等开关。
"""
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from simtrade import OrderManager

from .apifacts import BUY, SELL, assert_allowed
from .config import (DAILY_LOSS_FRACTION, MAX_ACTIVE_ORDERS, MAX_ORDER_QTY,
                     OPEN_CUTOFF, Limits)


class ReadOnlyError(RuntimeError):
    pass


class QueryBudget:
    """A14/A15：共享滑动窗口；包括 SDK 重连对账与 429 重试请求。"""
    def __init__(self, limits: Limits, clock=time.monotonic, sleep=time.sleep):
        self.limits = limits
        self.clock, self.sleep = clock, sleep
        self._http, self._heavy = deque(), deque()
        self._lock = threading.Lock()

    def acquire(self, heavy=False, block=True, reserve_http=False):
        while True:
            with self._lock:
                now = self.clock()
                for queue in (self._http, self._heavy):
                    while queue and now-queue[0] >= 60:
                        queue.popleft()
                recent = [t for t in self._http if now-t < 1]
                waits = [0.]
                per_second = max(1, self.limits.http_per_second*7//10) if reserve_http else self.limits.http_per_second
                per_minute = max(1, self.limits.http_per_minute*7//10) if reserve_http else self.limits.http_per_minute
                if len(recent) >= per_second:
                    waits.append(recent[0]+1-now)
                if len(self._http) >= per_minute:
                    waits.append(self._http[0]+60-now)
                if heavy and len(self._heavy) >= self.limits.heavy_per_minute:
                    waits.append(self._heavy[0]+60-now)
                wait = max(waits)
                if wait <= 0:
                    self._http.append(now)
                    if heavy:
                        self._heavy.append(now)
                    return True
                if not block:
                    return False
            self.sleep(wait + .001)


class ReadOnlyOrderManager(OrderManager):
    """继承本地官方 SDK；仅在传输入口增加预算和禁止写入约束。

_get/_post 是本地 SDK 3.0.1 的扩展接点，测试直接覆盖本版本兼容性。
保留 SDK 鉴权、推送恢复、对账和 429 处理，不重写网络客户端。
"""
    budget = None

    def _get(self, path, *args, **kwargs):
        if self.budget is not None:
            self.budget.acquire(heavy=path.split('?')[0] in ('/positions', '/orders', '/trades'))
        return super()._get(path, *args, **kwargs)

    def _post(self, path, *args, **kwargs):
        raise ReadOnlyError('阶段 1 禁止账户写请求')

    def add_order(self, *args, **kwargs):
        raise ReadOnlyError('阶段 1 禁止下单')

    def cancel_order(self, *args, **kwargs):
        raise ReadOnlyError('阶段 1 禁止撤单：不会改动网页端或其他进程委托')

    def cancel_all(self, *args, **kwargs):
        raise ReadOnlyError('阶段 1 禁止账户级撤单')


@dataclass(frozen=True)
class Intent:
    product_id: str
    side: str
    price: float
    qty: int


class SafetyGate:
    def __init__(self, limits: Limits, products: dict):
        self.limits, self.products = limits, products
        self.daily_orders_used = limits.daily_orders_used
        self.stopped = False
        self._messages = deque()
        self._lock = threading.Lock()

    def observe_net(self, net, day_start_net) -> list:
        if day_start_net is None or not math.isfinite(day_start_net) or day_start_net <= 0:
            return ['day_start_net_unknown']
        if net is None or not math.isfinite(net) or net <= 0:
            return ['net_unknown']
        if net <= day_start_net * (1 - DAILY_LOSS_FRACTION):
            self.stopped = True
        return ['daily_loss'] if self.stopped else []

    def check(self, intent: Intent, portfolio, now: datetime) -> list:
        """只检查必要安全闸；不能替代阶段 3 的联合压力检查器。"""
        product = self.products.get(intent.product_id, {})
        assert_allowed(product)
        errors = self.observe_net(portfolio.account.get('net'), portfolio.day_start_net)
        if intent.side not in (BUY, SELL):
            errors.append('side')
        prefix = 'buy' if intent.side == BUY else 'sell'
        qty = intent.qty
        if (type(qty) is not int or not 1 <= qty <= MAX_ORDER_QTY
                or product.get(prefix+'QtyLowerLimit') is None
                or product.get(prefix+'QtyUpperLimit') is None
                or not product[prefix+'QtyLowerLimit'] <= qty <= product[prefix+'QtyUpperLimit']):
            errors.append('quantity')
        price = intent.price
        tick, lower, upper = (product.get(k) for k in ('tickSize', 'priceLowerLimit', 'priceUpperLimit'))
        if (isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price)
                or any(x is None or not math.isfinite(x) for x in (tick, lower, upper))
                or tick <= 0 or not 0 < lower <= price <= upper
                or Decimal(str(price)) % Decimal(str(tick)) != 0):
            errors.append('price')
        if len(portfolio.active_orders) >= MAX_ACTIVE_ORDERS:
            errors.append('active_orders')
        if self.daily_orders_used >= self.limits.daily_cap:
            errors.append('daily_orders')
        if now.tzinfo is None:
            raise ValueError('检查时间必须携带时区')
        local = now.astimezone(ZoneInfo('Asia/Shanghai'))
        clock = (local.hour, local.minute, local.second, local.microsecond)
        in_session = ((9, 30, 0, 0) <= clock < (11, 30, 0, 0)
                      or (13, 0, 0, 0) <= clock < (15, 0, 0, 0))
        if local.weekday() >= 5 or not in_session:
            errors.append('session')  # 节假日仍由服务端判定；此处不伪造交易日历。
        frozen = portfolio.account.get('openFrozen')
        # 官方账户接口：显式 null 与 false 均表示未冻结；缺字段仍不能确认状态。
        if 'openFrozen' not in portfolio.account:
            errors.append('open_frozen_unknown')
        if (local.hour, local.minute) >= OPEN_CUTOFF or frozen:
            if type(qty) is not int or qty > portfolio.reducible_qty(intent.product_id, intent.side):
                errors.append('opening_forbidden')
        return errors

    def reserve_message(self, now=None, emergency=False) -> bool:
        """预留报/撤的一次额度。应急额度仍受账户总速率约束。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._messages and now-self._messages[0] >= 1:
                self._messages.popleft()
            limit = self.limits.throttle_per_second if emergency else self.limits.regular_per_second
            if len(self._messages) >= limit:
                return False
            self._messages.append(now)
            return True
