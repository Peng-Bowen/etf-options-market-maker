"""阶段二成交证据；A1/A2/A3：只从查询读取 fee/liquidity，不猜费率。"""
import copy
import time
from .probe_execution import ProbeBlocked


class TradeCollector:
    def __init__(self, sdk, state, journal):
        self.sdk, self.state, self.journal = sdk, state, journal
        self.page = 0
        self.date = None

    def poll(self, trade_date):
        """一次只取一页；每轮从 0 重扫，重复记录可补齐延迟入账的费用。"""
        if trade_date != self.date:
            self.page, self.date = 0, trade_date
        try:
            page = self.sdk.query_trades(date=trade_date, page=self.page)
        except ProbeBlocked:
            return False  # 不等待查询窗口，主循环先处理超时撤单。
        if page is None:
            self.journal.write('trade_query_error', dict(date=trade_date, page=self.page, error=self.sdk.last_error))
            return False
        self.journal.write('trades_page', dict(date=trade_date, page=self.page, data=page))
        for trade in page['items']:
            key = f"{trade['trade_date']}:{trade['exec_id']}"
            previous = self.state.data['trades'].get(key, {})
            # 已知字段不会因另一页的旧副本/null 退回未知；非空修订仍记录。
            merged = dict(previous, **{k:v for k,v in trade.items() if v is not None or k not in previous})
            if previous != merged:
                merged['observed_ms'] = int(time.time()*1000)+(self.journal.server_offset_ms or 0)
                self.state.data['trades'][key] = copy.deepcopy(merged)
                self.journal.write('trade_evidence', merged, trade.get('transact_time'))
        self.page = self.page+1 if page['has_more'] else 0
        self.state.save()
        return not page['has_more']
