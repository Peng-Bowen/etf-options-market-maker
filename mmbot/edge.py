"""以真实查询成交归因。未来价格仅作事后标签，绝不回写成交时输入。"""
import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo


class EdgeTracker:
    def __init__(self, data, taker_fee_per_lot, fair_tolerance_ms=5000,
                 min_lots=20, min_blocks=5, evidence_max_age_ms=172800000):
        if not math.isfinite(taker_fee_per_lot) or taker_fee_per_lot < 0:
            raise ValueError('退出手续费必须来自已核实成交，不能猜测')
        if min_lots < 2 or min_blocks < 2 or fair_tolerance_ms <= 0 or evidence_max_age_ms <= 0:
            raise ValueError('无效归因样本/时间参数')
        self.data = data
        self.fee = taker_fee_per_lot
        self.tolerance = fair_tolerance_ms
        self.min_lots, self.min_blocks = min_lots, min_blocks
        self.max_age = evidence_max_age_ms
        data.setdefault('edge', {})
        data.setdefault('fair_history', {})

    def add_fair(self, symbol, stamp, fair, bid, ask, multiplier):
        if not all(math.isfinite(v) and v > 0 for v in (fair,bid,ask,multiplier)) or bid > ask:
            return
        history = self.data['fair_history'].setdefault(symbol, [])
        if history and stamp <= history[-1][0]:
            return
        history.append([stamp, fair, bid, ask, multiplier])
        # 延迟超过保留窗口的查询成交将明确缺数据，不使用最近价冒充历史价。
        cutoff = stamp-900000
        while len(history) > 1 and history[1][0] < cutoff:
            history.pop(0)

    def refresh(self, now_ms):
        owned = {(a['trade_date'], a.get('order_id')) for a in self.data['attempts'].values()
                 if a.get('order_id') and a['status'] != 'conflict'}
        for key,t in self.data['trades'].items():
            if (t['trade_date'],t.get('order_id')) not in owned:
                continue
            stamp = t.get('transact_time')
            if stamp is None or stamp > now_ms or t.get('observed_ms',stamp) > now_ms:
                continue
            # 已完成记录保留原始取样点，历史环形缓冲淘汰后仍可补费用修订。
            old = self.data['edge'].get(key, {})
            history = self.data['fair_history'].get(t['product_id'], [])
            before = old.get('entry_sample')
            after = old.get('markout_sample')
            if after is not None and after[0] > now_ms:
                after = None
            if before is None:
                candidates = [x for x in history if stamp-self.tolerance <= x[0] <= stamp]
                before = candidates[-1] if candidates else None
            if after is None:
                candidates = [x for x in history if stamp+60000 <= x[0] <= min(now_ms,stamp+60000+self.tolerance)]
                after = candidates[0] if candidates else None
            valid = (type(t['last_qty']) is int and t['last_qty'] > 0 and t['side'] in ('1','2')
                     and isinstance(t['last_px'],(float,int)) and math.isfinite(t['last_px']) and t['last_px'] > 0)
            complete = bool(valid and before and after and t.get('fee') is not None and t.get('liquidity') in ('MAKER','TAKER'))
            row = dict(product_id=t['product_id'], trade_ms=stamp, evaluated_ms=now_ms,
                       qty=t['last_qty'], liquidity=t.get('liquidity'), complete=complete,
                       entry_sample=before, markout_sample=after, order_id=t['order_id'])
            if complete:
                if before[4] != after[4]:
                    row['complete'] = False
                    row['reason'] = '乘数发生变化，需人工核对'
                else:
                    side = 1 if t['side'] == '1' else -1
                    qty, multiplier = t['last_qty'], before[4]
                    spread = side*(before[1]-t['last_px'])*multiplier*qty
                    markout = side*(after[1]-before[1])*multiplier*qty
                    exit_spread = max(0., after[1]-after[2] if side == 1 else after[3]-after[1])
                    exit_cost = qty*(exit_spread*multiplier+self.fee)
                    row.update(spread=spread, markout=markout, rebate=max(-t['fee'],0.),
                               paid_fee=max(t['fee'],0.), exit_cost=exit_cost,
                               net_edge=spread+markout-t['fee']-exit_cost)
            self.data['edge'][key] = row

    def statistics(self, symbol, now_ms):
        rows = [r for r in self.data['edge'].values() if r['product_id'] == symbol and r['evaluated_ms'] <= now_ms
                and now_ms-self.max_age <= r['trade_ms'] <= now_ms-60000]
        maker = [r for r in rows if r['liquidity'] == 'MAKER']
        complete = [r for r in maker if r['complete']]
        lots = sum(r['qty'] for r in maker)
        complete_lots = sum(r['qty'] for r in complete)
        groups = {}
        for r in complete:
            group = groups.setdefault(r['trade_ms']//300000, [0.,0])
            group[0] += r['net_edge']; group[1] += r['qty']
        blocks = [net/qty for net,qty in groups.values()]
        mean = sum(r['net_edge'] for r in complete)/complete_lots if complete_lots else None
        # 五分钟分块的筛选下界，缓解同一秒/同笔拆成交重复计样本；不宣称消除了所有相关性。
        lower = statistics.mean(blocks)-1.96*statistics.stdev(blocks)/math.sqrt(len(blocks)) if len(blocks) >= 2 else None
        unknown = any(r['liquidity'] is None or (r['liquidity']=='MAKER' and not r['complete']) for r in rows)
        owned = {(a['trade_date'],a.get('order_id')):a for a in self.data['attempts'].values()
                 if a.get('order_id') and a['status'] != 'conflict'}
        cutoff_date = int(datetime.fromtimestamp((now_ms-self.max_age)/1000,ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d'))
        relevant = [t for t in self.data['trades'].values() if t['product_id']==symbol
                    and (t['trade_date'],t.get('order_id')) in owned and t['trade_date'] >= cutoff_date
                    and t.get('observed_ms',0) <= now_ms]
        if any(t.get('transact_time') is None for t in relevant):
            unknown = True
        # 已有成熟的成交回报却缺交易明细，也不能据残缺样本放量。
        for identity,a in owned.items():
            if a.get('product_id') != symbol or not a.get('cum_qty') or a['trade_date'] < cutoff_date:
                continue
            order_time = a.get('order',{}).get('transactionTime')
            if order_time is None or order_time <= now_ms-60000:
                queried_qty = sum(t['last_qty'] for t in relevant if (t['trade_date'],t['order_id']) == identity)
                if queried_qty < a['cum_qty']:
                    unknown = True
        eligible = bool(not unknown and complete_lots == lots and lots >= self.min_lots
                        and len(blocks) >= self.min_blocks and mean is not None and mean > 0 and lower > 0)
        span = max(1., (max((r['trade_ms'] for r in complete),default=now_ms)
                        -min((r['trade_ms'] for r in complete),default=now_ms))/1000)
        return dict(as_of_ms=now_ms, maker_lots=lots, complete_lots=complete_lots, blocks=len(blocks),
                    net_edge_per_lot=mean, lower_bound=lower, eligible=eligible,
                    score=(lower*lots/span if eligible else 0.), incomplete=unknown)

    def report(self, path, now_ms):
        symbols = sorted({t['product_id'] for t in self.data['trades'].values()})
        rows = ['# 成交经济性报告', '', '净 Maker Edge 为 1 分钟估值收益减退出成本估计，不是已实现利润。', '',
                '|合约|Maker 张数|完整张数|5 分钟块数|每张净 Edge|分块下界|扩覆盖准入|',
                '|---|---:|---:|---:|---:|---:|---|']
        for symbol in symbols:
            s = self.statistics(symbol,now_ms)
            rows.append(f"|{symbol}|{s['maker_lots']}|{s['complete_lots']}|{s['blocks']}|{s['net_edge_per_lot']}|{s['lower_bound']}|{s['eligible']}|")
        rows += ['', '分解和取样时间见状态文件 edge；缺失费用、角色或时点数据不按零处理。',
                 '未来一分钟数据只用于成熟后的评估。日收益以系统 account.net 为准。']
        path.write_text('\n'.join(rows)+'\n',encoding='utf-8')
