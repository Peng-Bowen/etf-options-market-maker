"""阶段三/四运行控制：先收回报，再独立检查，再发订单。默认入口仍只读。"""
import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .apifacts import round_to_tick
from .attribution import TradeCollector
from .config import Limits
from .coverage import CoveragePolicy, CoverageController
from .edge import EdgeTracker
from .execution import Intent
from .pricing import PricingError
from .probe_execution import ProbeBlocked, TERMINAL
from .probe_state import ProbeState
from .quoter import QuotePolicy, make_quotes, order_diff
from .risk import RiskChecker
from .trading_execution import Admission

SHANGHAI = ZoneInfo('Asia/Shanghai')


@dataclass(frozen=True)
class StrategySettings:
    stage: int
    symbols: tuple
    quote: QuotePolicy
    coverage: CoveragePolicy
    probe_state_path: Path
    confirmations: dict
    edge: dict

    def __post_init__(self):
        if self.stage not in (3,4) or not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError('阶段或合约列表无效')
        if self.stage == 3 and (len(self.symbols) != 1 or self.coverage.max_symbols != 1):
            raise ValueError('阶段三仅允许单合约、每侧 1 张')
        if self.edge.get('min_lots',20) < 20 or self.edge.get('min_blocks',5) < 5:
            raise ValueError('正式扩覆盖至少需要 20 张、5 个时间块的实测样本')


def load_strategy_config(path):
    return parse_strategy_config(json.loads(path.read_text(encoding='utf-8')),path.parent)


def parse_strategy_config(raw,base_dir):
    stage = raw['stage']
    symbols = tuple(raw['symbols'])
    if stage not in (3,4) or not symbols or len(set(symbols)) != len(symbols) or any(not isinstance(s,str) for s in symbols):
        raise ValueError('阶段必须为 3/4，合约列表须明确且不重复')
    if stage == 3 and len(symbols) != 1:
        raise ValueError('阶段三只允许一个合约')
    coverage = CoveragePolicy(**raw['coverage'])
    if stage == 3 and coverage.max_symbols != 1:
        raise ValueError('阶段三 max_symbols 必须为 1')
    probe_path = Path(raw['probe_state_path']) if raw.get('probe_state_path') else None
    if probe_path is not None and not probe_path.is_absolute():
        probe_path = base_dir/probe_path
    return StrategySettings(stage,symbols,QuotePolicy(**raw['quote']),coverage,probe_path,
                            raw['confirmations'],raw['edge'])


class StrategySession:
    def __init__(self, settings, sdk, recorder, monitor, gate, info, config, connected):
        self.settings, self.sdk, self.recorder, self.monitor = settings, sdk, recorder, monitor
        self.gate, self.config, self.connected = gate, config, connected
        self.date = info['trade_date']
        self.recovered = self.ever_ready = self.halting = self.snapshot_ready = False
        self.generation = 0
        self.processed_generation = -1
        self.last_poll = self.last_report = self.last_fair = 0.
        self.account_generation = 0
        self.selected = []
        self.reasons = []
        self.start_ms = self.now_ms()
        self.periods = {}
        self.failures = 0
        if monitor is None:
            raise ValueError('正式策略必须同时提供定价/风控配置')
        if any(s not in recorder.universe.options for s in settings.symbols):
            raise ValueError('策略含不在白名单参考数据中的合约')
        probe_path = settings.probe_state_path or config.data_dir/f"probe-state-{info['account_id']}.json"
        if not probe_path.exists():
            raise ValueError(f'未找到本账户探针记录：{probe_path}；请先运行阶段二')
        probe_data = json.loads(probe_path.read_text(encoding='utf-8'))
        self.admission = Admission(probe_data,info['account_id'],settings.confirmations,self.start_ms)
        self.state = ProbeState(config.data_dir/f"strategy-state-{info['account_id']}.json", info['account_id'])
        try:
            baseline = self.state.baseline(self.date,config.day_start_net)
            if baseline is None:
                raise ValueError('必须提供经过核实的当日期初净值 TRADING_DAY_START_NET')
            recorder.portfolio.day_start_net = baseline
            self.state.data.setdefault('stage3_sessions', [])
            self.state.data.setdefault('daily_performance', {})
            self.state.data['limits'][str(self.date)] = dict(info)
            self.edge = EdgeTracker(self.state.data,self.admission.taker_fee,**settings.edge)
            self.coverage = CoverageController(settings.coverage)
            previous = self.state.data.get('coverage_state',{})
            self.coverage.active = [s for s in previous.get('active',[]) if s in settings.symbols]
            self.coverage.since = {s:previous.get('since',{}).get(s,self.start_ms) for s in self.coverage.active}
            self.collector = TradeCollector(sdk,self.state,recorder.journal)
            if settings.stage == 4:
                verified = [s for s in self.state.data['stage3_sessions'] if s['passed'] and s['end_ms'] <= self.start_ms]
                self.edge.refresh(self.start_ms)
                eligible = [s for s in settings.symbols if self.edge.statistics(s,self.start_ms)['eligible']
                            and any(s == v['symbol'] for v in verified)]
                if not eligible:
                    raise ValueError('阶段四需要本账户已完成阶段三时段验收且净 Maker Edge 实测为正的合约')
            sdk.configure(gate,recorder.portfolio,recorder.market,self.state,recorder.journal,
                          self.date,5000,self.now,connected)
            sdk.attach_strategy(monitor.engine,monitor.limits,self.admission,self.ready,self.permitted,settings.stage)
            for raw in list(recorder.portfolio.orders.values()):
                sdk.on_order(raw)
            recorder.listeners.append(self.on_event)
            self.state.save()
        except Exception:
            self.state.close()
            raise

    def now_ms(self):
        return int(time.time()*1000)+(self.recorder.journal.server_offset_ms or 0)

    def now(self):
        return datetime.fromtimestamp(self.now_ms()/1000,SHANGHAI)

    def ready(self):
        return self.recovered and self.connected() and self.snapshot_ready and not self.halting

    def permitted(self, symbol, now_ms):
        if self.settings.stage == 3:
            return symbol == self.settings.symbols[0]
        qualified = any(s['passed'] and s['symbol']==symbol and s['end_ms'] <= now_ms
                        for s in self.state.data['stage3_sessions'])
        return symbol in self.selected and qualified and self.edge.statistics(symbol,now_ms)['eligible']

    def on_event(self, kind, raw, op, mono, wall):
        if kind == 'order_recovery':
            if raw.get('reconnected') or raw.get('truncated'):
                self.halting = True
                self.failures += 1
            else:
                self.recovered = True
        elif kind == 'order':
            self.sdk.on_order(raw)
            self.generation += 1
            if self.sdk.ledger_dirty:
                self.snapshot_ready = False
        elif kind == 'position':
            self.snapshot_ready = False
            self.sdk.ledger_dirty = True
            self.sdk.fill_generation += 1
        elif kind == 'account':
            self.account_generation += 1
        elif kind == 'market':
            relevant = set(self.settings.symbols) | {self.recorder.universe.options[s]['underlying'] for s in self.settings.symbols}
            if raw.get('productId') in relevant:
                self.generation += 1

    def refresh_snapshot(self):
        """事件触发查询，不轮询委托。查询期间有新成交/持仓推送则重做快照。"""
        generation = self.sdk.fill_generation
        account_generation = self.account_generation
        positions = self.sdk.query_positions()
        account = self.sdk.query_account()
        info = self.sdk.me()
        self.recorder.drain()
        if positions is None or account is None or info is None:
            raise ProbeBlocked('账户快照查询失败')
        if info['trade_date'] != self.date or not info['api_trading_enabled']:
            self.halting = True
            raise ProbeBlocked('交易日/账户权限变化')
        if generation != self.sdk.fill_generation:
            raise ProbeBlocked('查询期间持仓变化，等待一致快照')
        self.recorder.portfolio.positions = {p['security_id']:dict(p['raw']) for p in positions}
        if account_generation == self.account_generation:
            self.recorder.portfolio.account = dict(account['raw'])
        self.gate.limits = Limits.from_me(info)
        self.sdk.budget.limits = self.gate.limits
        self.gate.daily_orders_used = max(self.gate.daily_orders_used,info['daily_orders_used'])
        self.sdk.ledger_dirty = False
        self.snapshot_ready = True
        self.recorder.journal.write('strategy_snapshot',dict(account=account,positions=positions,me=info))

    def record_fair(self, now_ms):
        for symbol in self.settings.symbols:
            try:
                value = self.monitor.engine.value(symbol,now_ms)
                book = self.recorder.market.books.get(symbol)
                if book and book.is_fresh(now_ms,self.monitor.engine.config.max_book_age_ms):
                    self.edge.add_fair(symbol,now_ms,value.price,book.bid_px,book.ask_px,value.multiplier)
            except (PricingError,KeyError):
                continue

    def current_risk(self, now_ms):
        joint = self.sdk._joint_portfolio()
        symbols = {p['securityId'] for p in joint.positions.values() if p.get('longQty',0) or p.get('shortQty',0)}
        symbols |= {o['productId'] for o in joint.active_orders}
        values = {s:self.monitor.engine.value(s,now_ms) for s in symbols}
        checker = RiskChecker(values,self.monitor.limits,now_ms,
                              max_underlying_age_ms=self.monitor.engine.config.max_book_age_ms,
                              max_iv_age_ms=self.monitor.engine.config.max_iv_age_ms)
        result = checker.evaluate(list(joint.positions.values()),joint.active_orders,[])
        ok,reasons = checker.check_hard_limits(list(joint.positions.values()),joint.active_orders,[])
        if not ok:
            self.failures += 1
            raise ProbeBlocked('当前联合敞口越限：'+','.join(reasons))
        return result

    def choose_coverage(self, now_ms, risk):
        if self.settings.stage == 3:
            self.selected = list(self.settings.symbols)
            return
        evidence = {s:self.edge.statistics(s,now_ms) for s in self.settings.symbols}
        for symbol,row in evidence.items():
            row['eligible'] &= any(v['passed'] and v['symbol']==symbol and v['end_ms'] <= now_ms
                                   for v in self.state.data['stage3_sessions'])
        stranded = {}
        for p in self.recorder.portfolio.positions.values():
            symbol = p['securityId']
            try:
                value = self.monitor.engine.value(symbol,now_ms)
                book = self.recorder.market.books[symbol]
                net = p['longQty']-p['shortQty']
                spread = max(0.,value.price-book.bid_px if net>0 else book.ask_px-value.price)
                stranded[symbol] = abs(net)*(spread*value.multiplier+self.admission.taker_fee)
            except (PricingError,KeyError):
                stranded[symbol] = math.inf
        selected = self.coverage.select(evidence,now_ms,stranded)
        if selected != self.selected:
            self.generation += 1
        self.selected = selected
        limit = self.monitor.limits.max_loss
        utilization = risk.worst_loss/limit if limit else (math.inf if risk.worst_loss else 0.)
        self.coverage.adjust(now_ms,utilization,bool(selected))
        self.sdk.soft_limits = self.coverage.limits(self.monitor.limits)
        soft = self.sdk.soft_limits
        if (risk.worst_loss > soft.max_loss or risk.short_lots > soft.max_short_lots
                or risk.cash_delta > soft.max_cash_delta_per_underlying
                or risk.cash_gamma > soft.max_cash_gamma or risk.vega > soft.max_vega):
            self.cancel_all_own()

    def cancel_all_own(self):
        refs = [a['client_ref'] for a in self.state.data['attempts'].values() if a['status'] not in TERMINAL]
        if refs:
            self.sdk.request_cancel(refs)

    def reduce_inventory(self):
        # 先撤报价并等终态；不假定撤单途中对手不会成交。
        live = [a for a in self.state.data['attempts'].values() if a['status'] not in TERMINAL]
        if live:
            quotes = [a['client_ref'] for a in live if a['probe'] != 'REDUCE']
            if quotes:
                self.sdk.request_cancel(quotes)
            return
        if not self.snapshot_ready or self.sdk.ledger_dirty:
            self.refresh_snapshot()
        for p in self.recorder.portfolio.positions.values():
            net = p['longQty']-p['shortQty']
            if not net:
                continue
            symbol = p['securityId']
            b = self.recorder.market.books.get(symbol)
            if not b:
                continue
            side = '2' if net>0 else '1'
            price = round_to_tick(b.bid_px if side=='2' else b.ask_px,
                                  self.gate.products[symbol]['tickSize'],side)
            result = self.sdk.submit_batch([Intent(symbol,side,price,1)],reducing=True)
            if result:
                self.snapshot_ready = False
            return

    def note_session(self, now_ms, submitted=False):
        if self.settings.stage != 3:
            return
        today = self.now().replace(hour=0,minute=0,second=0,microsecond=0)
        for label,start_h,start_m,end_h,end_m in (('AM',9,30,11,30),('PM',13,0,15,0)):
            start = int(today.replace(hour=start_h,minute=start_m).timestamp()*1000)
            end = int(today.replace(hour=end_h,minute=end_m).timestamp()*1000)
            quote_end = end-180000 if label == 'PM' else end  # 下午 14:57 起按规则减仓。
            key = f'{self.date}:{label}'
            if submitted and start <= now_ms < end:
                self.periods.setdefault(key,dict(first_quote_ms=now_ms,failures_start=self.failures,
                                                 rates_start=self.sdk.rate_limit_count,
                                                 covered_ms=0,last_observation_ms=now_ms))
            if key in self.periods and not self.periods[key].get('recorded'):
                period = self.periods[key]
                live_sides = {a['side'] for a in self.state.data['attempts'].values()
                              if a['product_id']==self.settings.symbols[0] and a['status'] in ('working','partial')
                              and not a.get('cancel_required') and a['probe']=='MM'}
                book = self.recorder.market.books.get(self.settings.symbols[0])
                if live_sides == {'1','2'} and book and book.is_fresh(now_ms,self.monitor.engine.config.max_book_age_ms):
                    period['covered_ms'] += max(0,min(1000,min(now_ms,quote_end)-period['last_observation_ms']))
                period['last_observation_ms'] = now_ms
            if now_ms >= end and key in self.periods and not self.periods[key].get('recorded'):
                period = self.periods[key]
                passed = (self.start_ms <= start+60000 and period['first_quote_ms'] <= start+60000
                          and self.failures == period['failures_start'] and not self.halting
                          and self.sdk.rate_limit_count == period['rates_start']
                          and period['covered_ms'] >= quote_end-start-60000
                          and not any(a['status']=='unknown' for a in self.state.data['attempts'].values()))
                self.state.data['stage3_sessions'].append(dict(key=key,symbol=self.settings.symbols[0],
                    end_ms=end,quote_end_ms=quote_end,passed=passed,first_quote_ms=period['first_quote_ms'],
                    covered_ms=period['covered_ms'],failures=self.failures-period['failures_start']))
                period['recorded'] = True
                self.state.save()

    def step(self):
        now_ms = self.now_ms()
        now = self.now()
        if int(now.strftime('%Y%m%d')) != self.date:
            self.halting = True
        if self.ever_ready and not self.connected():
            if not self.halting:
                self.failures += 1
            self.halting = True
        issues = self.gate.observe_net(self.recorder.portfolio.account.get('net'),self.recorder.portfolio.day_start_net)
        self.sdk.remember_stop()
        if self.gate.stopped:
            self.halting = True
        self.sdk.expire(now_ms,all_orders=self.halting)
        mono = time.monotonic()
        if mono-self.last_fair >= 1:
            self.record_fair(now_ms)
            self.edge.refresh(now_ms)
            self.last_fair = mono
        if mono-self.last_poll >= max(5.,120/self.gate.limits.heavy_per_minute):
            self.last_poll = mono
            self.collector.poll(self.date)
            self.edge.refresh(now_ms)
            self.generation += 1
        self.note_session(now_ms)
        net = self.recorder.portfolio.account.get('net')
        if net is not None:
            self.state.data['daily_performance'][str(self.date)] = dict(net=net,as_of_ms=now_ms,
                start_net=self.recorder.portfolio.day_start_net,return_to_date=net/self.recorder.portfolio.day_start_net-1)
        try:
            if self.halting or issues:
                self.cancel_all_own()
                return
            if not self.recovered or not self.connected():
                return
            self.ever_ready = True
            clock = (now.hour,now.minute,now.second)
            if ((11,29,55) <= clock < (13,0,0) or clock >= (14,59,55) or clock < (9,30,0)):
                self.cancel_all_own()
                return
            if not self.snapshot_ready or self.sdk.ledger_dirty:
                self.refresh_snapshot()
            now_ms = self.now_ms()
            now = self.now()
            risk = self.current_risk(now_ms)
            self.choose_coverage(now_ms,risk)
            cutoff = (14,55) if self.settings.stage==4 else (14,57)
            if (now.hour,now.minute) >= cutoff:
                self.reduce_inventory()
                return
            if self.generation == self.processed_generation:
                return
            desired = []
            for symbol in self.selected:
                try:
                    value = self.monitor.engine.value(symbol,now_ms)
                    b = self.recorder.market.books[symbol]
                    if b.is_fresh(now_ms,self.monitor.engine.config.max_book_age_ms):
                        desired += make_quotes(value,b,self.gate.products[symbol],self.settings.quote)
                except (PricingError,KeyError):
                    continue
            cancels,additions = order_diff(desired,self.state.data['attempts'].values(),self.gate.products,self.settings.quote,now_ms)
            self.processed_generation = self.generation
            if cancels:
                self.sdk.request_cancel(cancels)
                return
            if additions:
                # 简单优先级：优先对冲已有净持仓；独立检查逐个筛选，再整批复核。
                def priority(q):
                    p = self.recorder.portfolio.positions.get(q.product_id,{})
                    net = p.get('longQty',0)-p.get('shortQty',0)
                    return 0 if (net>0 and q.side=='2') or (net<0 and q.side=='1') else 1
                chosen = []
                for intent in sorted(additions,key=priority):
                    try:
                        self.sdk.validate_batch(chosen+[intent])
                        chosen.append(intent)
                    except (ProbeBlocked,PricingError,KeyError):
                        continue
                result = self.sdk.submit_batch(chosen)
                self.snapshot_ready = False
                self.note_session(now_ms,submitted=bool(result))
                if self.sdk.last_code in (14,429):
                    self.failures += 1
        except (ProbeBlocked,PricingError,KeyError) as exc:
            reason = str(exc)
            if self.reasons != [reason]:
                self.recorder.journal.write('strategy_blocked',dict(reason=reason))
            self.reasons = [reason]
            # 风控失败和缺少定价时先撤现有报价；不自动绕过硬约束强平。
            if not isinstance(exc,ProbeBlocked) or '越限' in reason:
                self.cancel_all_own()
        finally:
            if mono-self.last_report >= 5:
                self.report(now_ms)
                self.last_report = mono

    def report(self, now_ms):
        self.state.data['coverage_state'] = dict(active=self.coverage.active,since=self.coverage.since,scale=self.coverage.scale)
        self.edge.report(self.config.data_dir/'strategy_report.md',now_ms)
        self.recorder.journal.write('strategy_status',dict(stage=self.settings.stage,selected=self.selected,
            budget_scale=self.coverage.scale if self.settings.stage==4 else 1.,halted=self.halting,
            ledger_dirty=self.sdk.ledger_dirty,reasons=self.reasons,
            unresolved=[a['client_ref'] for a in self.state.data['attempts'].values() if a['status'] not in TERMINAL]))
        self.state.save()

    def shutdown(self, seconds=15):
        self.halting = True
        deadline = time.monotonic()+seconds
        while True:
            self.recorder.drain()
            self.sdk.expire(all_orders=True)
            live = [a for a in self.state.data['attempts'].values() if a['status'] not in TERMINAL]
            if not live or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        self.collector.poll(self.date)
        self.edge.refresh(self.now_ms())
        self.report(self.now_ms())

    def close(self):
        self.recorder.listeners.remove(self.on_event)
        self.state.close()
