"""简单被动报价与差分。只产生意图，不决定是否满足风险约束。"""
from dataclasses import dataclass
from decimal import Decimal

from .apifacts import round_to_tick
from .execution import Intent
from .probe_execution import TERMINAL


@dataclass(frozen=True)
class QuotePolicy:
    half_spread_ticks: int
    reprice_ticks: int
    min_rest_ms: int

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in vars(self).values()):
            raise ValueError('报价价差、改价阈值和存续时间须为正整数')


def make_quotes(value, book, product, policy):
    if not book.is_valid:
        return []
    tick = product['tickSize']
    bid = round_to_tick(max(tick, min(book.bid_px, value.price-policy.half_spread_ticks*tick)), tick, '1')
    ask = round_to_tick(max(book.ask_px, value.price+policy.half_spread_ticks*tick), tick, '2')
    # 对两侧都保留 ±10% 规则，不能靠调开关绕开比赛报价要求。
    if not (product['priceLowerLimit'] <= bid < ask <= product['priceUpperLimit']
            and .9*book.bid_px <= bid and ask <= 1.1*book.bid_px
            and bid < book.ask_px and ask > book.bid_px):
        return []
    return [Intent(value.product_id, '1', bid, 1), Intent(value.product_id, '2', ask, 1)]


def order_diff(desired, attempts, products, policy, now_ms):
    """同侧有未知/待撤单时不补单；有撤单动作的这一轮完全不新增。"""
    targets = {(q.product_id, q.side): q for q in desired}
    live = [a for a in attempts if a['status'] not in TERMINAL]
    cancels, occupied = [], set()
    for a in live:
        key = (a['product_id'], a['side'])
        occupied.add(key)
        target = targets.get(key)
        if a.get('cancel_required'):
            continue
        if target is None:
            cancels.append(a['client_ref'])
        elif a['status'] != 'unknown' and now_ms-a['sent_ms'] >= policy.min_rest_ms:
            ticks = abs(Decimal(str(target.price))-Decimal(str(a['price']))) / Decimal(str(products[a['product_id']]['tickSize']))
            if ticks >= policy.reprice_ticks or a['qty'] != target.qty:
                cancels.append(a['client_ref'])
    additions = [q for key,q in targets.items() if key not in occupied]
    return cancels, [] if cancels else additions
