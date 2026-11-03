"""可配置的是连接和观察参数；安全上限不暴露为环境变量。"""
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .apifacts import FUTURE_FAMILIES

MAX_ORDER_QTY = 5
MAX_ACTIVE_ORDERS = 20
MAX_DAILY_ORDERS = 2000
DAILY_LOSS_FRACTION = .005
OPEN_CUTOFF = (14, 57)
STALE_MS = 5000  # 观察阈值，不是对接口更新频率的假设。
EVENT_QUEUE_SIZE = 50000


@dataclass(frozen=True)
class Limits:
    throttle_per_second: int
    daily_order_limit: int
    daily_orders_used: int
    heavy_per_minute: int
    http_per_second: int
    http_per_minute: int
    ws_max_connections: int
    ws_connections: int

    @classmethod
    def from_me(cls, info: dict) -> 'Limits':
        values = {}
        for name in cls.__dataclass_fields__:
            value = info.get(name)
            minimum = 0 if name in ('daily_order_limit', 'daily_orders_used', 'ws_connections') else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f'/me 缺少有效限额：{name}')
            values[name] = value
        return cls(**values)

    @property
    def daily_cap(self) -> int:
        return min(self.daily_order_limit * 8 // 10, MAX_DAILY_ORDERS) if self.daily_order_limit else MAX_DAILY_ORDERS

    @property
    def regular_per_second(self) -> int:
        return self.throttle_per_second * 7 // 10


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str = field(repr=False)
    futures: tuple
    data_dir: Path = Path('data')
    day_start_net: float = None

    @classmethod
    def from_env(cls, env=None) -> 'Config':
        env = os.environ if env is None else env
        url = env.get('TRADING_GATEWAY_URL', '').strip().rstrip('/')
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.netloc or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ('', '/gateway')):
            raise ValueError('TRADING_GATEWAY_URL 必须是学生站根地址或 /gateway 地址')
        key = env.get('TRADING_API_KEY', '').strip()
        if not key:
            raise ValueError('缺少环境变量 TRADING_API_KEY')
        futures = tuple(dict.fromkeys(x.strip() for x in env.get('TRADING_FUTURES', '').split(',') if x.strip()))
        if (any(not re.fullmatch(r'(IF|IH|IM|IC)\d{4}\.CFE', s) for s in futures)
                or {s[:2] for s in futures} != set(FUTURE_FAMILIES)):
            raise ValueError('TRADING_FUTURES 需提供四个品种的实际到期合约，以逗号分隔')
        baseline = env.get('TRADING_DAY_START_NET')
        baseline = float(baseline) if baseline else None
        if baseline is not None and (not math.isfinite(baseline) or baseline <= 0):
            raise ValueError('TRADING_DAY_START_NET 必须为当日期初净值的正数')
        return cls(f'{parts.scheme}://{parts.netloc}', key, futures,
                   Path(env.get('TRADING_DATA_DIR', 'data')), baseline)

    @property
    def gateway_url(self) -> str:
        return self.base_url + '/gateway'

    @property
    def md_url(self) -> str:
        return self.base_url.replace('https://', 'wss://').replace('http://', 'ws://') + '/mdws'

    @property
    def order_url(self) -> str:
        return self.md_url.rsplit('/', 1)[0] + '/orderws'
