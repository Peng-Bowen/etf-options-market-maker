"""API 文档中的确定语义。A 编号对应 Codex任务说明第 2 章。"""
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

ALLOWED_UNDERLYINGS = ('510050.SH', '510300.SH', '159919.SZ', '159915.SZ')
FUTURE_FAMILIES = ('IF', 'IH', 'IM', 'IC')
CROSS_TYPE_MAKER, CROSS_TYPE_TAKER = 1, 2  # A2
LIQUIDITY_MAKER, LIQUIDITY_TAKER = 'MAKER', 'TAKER'
FINAL_STATUSES = frozenset(('2', '4', '8'))  # A6；X 不是已确认的服务端终态。
MAX_SUBSCRIPTIONS = 2000  # A17，协议上限，不是账户示例限额。
BUY, SELL = '1', '2'


def trade_push_time_to_epoch_ms(utc_yyyymmddhhmmss: int) -> int:
    """A2：只用于成交推送；委托和查询的时间已经是 epoch 毫秒。"""
    dt = datetime.strptime(str(utc_yyyymmddhhmmss), '%Y%m%d%H%M%S')
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def lots_to_units(product: dict, lots: int) -> int:
    if type(lots) is not int or lots <= 0:
        raise ValueError('手数必须为正整数')
    kind = product['securityType']
    if kind in ('OPT', 'FUT'):  # A4
        return lots
    if kind in ('CS', 'FND'):
        return lots * int(product['buyRoundLot'])
    raise ValueError('未知证券类型')


def round_to_tick(price: float, tick: float, side: str) -> float:
    if side not in (BUY, SELL) or not all(math.isfinite(x) and x > 0 for x in (price, tick)):
        raise ValueError('无效的价格、tick 或方向')
    p, t = Decimal(str(price)), Decimal(str(tick))
    rounding = ROUND_FLOOR if side == BUY else ROUND_CEILING
    return float((p/t).to_integral_value(rounding=rounding)*t)


def assert_allowed(product: dict) -> None:
    """不用 Python assert，避免 python -O 移除交易白名单检查。A29。"""
    if product.get('securityType') != 'OPT' or product.get('underlying') not in ALLOWED_UNDERLYINGS:
        raise ValueError('仅允许四个 ETF 标的下的期权')
