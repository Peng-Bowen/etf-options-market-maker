"""L1 快照。接收时间与行情时间分开，不从 last 推断成交流。"""
import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Book:
    product_id: str
    bid_px: float
    bid_size: int
    ask_px: float
    ask_size: int
    last: float
    timestamp_ms: int
    recv_monotonic: float

    @property
    def is_valid(self) -> bool:
        # 双边可报价性；原始单边行情仍保存在 journal 中。
        return (self.timestamp_ms > 0 and math.isfinite(self.bid_px) and math.isfinite(self.ask_px)
                and 0 < self.bid_px <= self.ask_px and self.bid_size > 0 and self.ask_size > 0)

    @property
    def age_ms(self) -> float:
        return max(0., (time.monotonic() - self.recv_monotonic)*1000)

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        return self.is_valid and self.age_ms <= max_age_ms and 0 <= now_ms-self.timestamp_ms <= max_age_ms


class MarketData:
    def __init__(self):
        self.books = {}

    def update(self, raw: dict, recv_monotonic=None) -> Book:
        symbol = raw['productId']
        timestamp = int(raw.get('timestamp') or 0)  # A19
        old = self.books.get(symbol)
        if old is not None and timestamp > 0 and timestamp <= old.timestamp_ms:
            return old  # 重连旧快照不得把本地年龄归零。
        book = Book(symbol, float(raw.get('bidPx') or 0), int(raw.get('bidSize') or 0),
                    float(raw.get('askPx') or 0), int(raw.get('askSize') or 0),
                    float(raw.get('last') or 0), timestamp,
                    time.monotonic() if recv_monotonic is None else recv_monotonic)
        self.books[symbol] = book
        return book
