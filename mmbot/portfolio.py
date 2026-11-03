"""原始推送格式的只读账本。风险计算和归因由后续阶段负责。"""
from .apifacts import FINAL_STATUSES, BUY


class Portfolio:
    def __init__(self):
        self.account = {}
        self.positions = {}
        self.orders = {}
        self.day_start_net = None

    def apply_order(self, raw: dict) -> None:
        order_id = str(raw['orderId'])
        old = self.orders.get(order_id)
        if old:
            if old.get('ordStatus') in FINAL_STATUSES and raw.get('ordStatus') not in FINAL_STATUSES:
                return
            if int(raw.get('cumQty', 0)) < int(old.get('cumQty', 0)):
                return
            old_time = old.get('transactionTime') or old.get('transactTime') or 0
            new_time = raw.get('transactionTime') or raw.get('transactTime') or 0
            if new_time < old_time:
                return
        self.orders[order_id] = dict(old or {}, **raw)

    @property
    def active_orders(self) -> list:
        return [o for o in self.orders.values() if o.get('ordStatus') not in FINAL_STATUSES]

    def reducible_qty(self, symbol: str, side: str) -> int:
        """A28：可平量未知时不声称能够减仓；同向未完成委托先占用额度。"""
        p = self.positions.get(symbol, {})
        available = p.get('sellableQty')
        if available is None:
            return 0
        net = int(p.get('longQty', 0)) - int(p.get('shortQty', 0))
        opposite = max(0, -net if side == BUY else net)
        reserved = sum(int(o.get('leavesQty', o.get('orderQty', 0))) for o in self.active_orders
                       if o.get('productId') == symbol and str(o.get('side')) == side)
        return max(0, min(int(available), opposite - reserved))
