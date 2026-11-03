"""只查询文档支持的接口；不猜测合约代码或缺失参数。"""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .apifacts import ALLOWED_UNDERLYINGS, assert_allowed


@dataclass
class Universe:
    options: dict
    futures: dict

    @property
    def symbols(self) -> list:
        return sorted(set(self.options) | set(self.futures) | set(ALLOWED_UNDERLYINGS))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(dict(options=list(self.options.values()),
                                       futures=list(self.futures.values()),
                                       underlyings=ALLOWED_UNDERLYINGS), ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)


def load_universe(manager, futures: tuple, budget=None) -> Universe:
    options, anchors = {}, {}
    for underlying in ALLOWED_UNDERLYINGS:
        if budget:
            budget.acquire()
        rows = manager.query_ref_data_by_underlying(underlying)  # A23
        if manager.last_error:
            raise RuntimeError(f'合约查询失败：{underlying}：{manager.last_error}')
        selected = [r for r in rows if r.get('securityType') == 'OPT' and r.get('underlying') == underlying]
        if not selected:
            raise ValueError(f'{underlying} 没有可核对的期权合约')
        for row in selected:
            assert_allowed(row)
            for field in ('tickSize', 'contractMultiplier', 'strikePx', 'maturityDate'):
                value = row.get(field)
                if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                    raise ValueError(f'{row.get("productId")} 缺少有效 {field}')
            if row.get('callOrPut') not in ('C', 'P') or row.get('sellMargin') is None:
                raise ValueError(f'{row.get("productId")} 期权参考数据不完整')
            options[row['productId']] = dict(row)
    for symbol in futures:
        if not re.fullmatch(r'(IF|IH|IM|IC)\d{4}\.CFE', symbol):
            raise ValueError('不允许的期货行情代码')
        if budget:
            budget.acquire()
        row = manager.query_ref_data(symbol)
        if manager.last_error or row.get('productId') != symbol or row.get('securityType') != 'FUT':
            raise ValueError(f'无法核实期货行情合约 {symbol}')
        anchors[symbol] = dict(row)
    return Universe(options, anchors)
