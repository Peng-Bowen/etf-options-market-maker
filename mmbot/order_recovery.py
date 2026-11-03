"""仅对未决委托进行有界恢复；正常工作单不轮询。

原单仍有效时允许同 ref、同参数重试；过期/停机/跨日只查询。
失踪判定沿用 SDK 的双完整查询、至少 60 秒间隔和至少 300 秒等待。
"""
import threading
import time

from simtrade import OrderManager

from .execution import Intent

RECOVERY_INTERVAL_MS = 5000
MAX_RECOVERY_RETRIES = 2


class OrderRecoveryMixin:
    def _risk_attempts(self):
        excluded = getattr(self,'_recovery_exclude_ref',None)
        return {k:a for k,a in self.state.data['attempts'].items() if a['client_ref'] != excluded}

    @staticmethod
    def _request_payload(attempt):
        if 'request' in attempt:
            return dict(attempt['request'])
        probe = attempt['probe']
        note = f'{probe} 开盘探针 1 张' if probe.startswith('P') else f'{probe} 策略委托 1 张'
        return dict(productId=attempt['product_id'],side=attempt['side'],ordType='2',
                    price=attempt['price'],orderQty=attempt['qty'],timeInForce='0',
                    clientRef=attempt['client_ref'],note=note)

    def _retry_unknown(self, attempt, now_ms):
        from .probe_execution import ProbeBlocked, TIMEOUT_MS
        if (attempt.get('order_id') or attempt.get('cum_qty') or attempt.get('cancel_required')
                or attempt.get('recovery_retries',0) >= MAX_RECOVERY_RETRIES
                or now_ms >= attempt['sent_ms']+TIMEOUT_MS
                or time.monotonic()-attempt['sent_monotonic'] >= TIMEOUT_MS/1000
                or attempt['trade_date'] != int(self.now().strftime('%Y%m%d'))
                or self.gate.stopped or not self.connected()):
            return
        with self._submit_lock:
            try:
                # 用原单替换原先的未知占用，不重复计算同一笔单，也不释放其他敞口。
                self._recovery_exclude_ref = attempt['client_ref']
                self._check(attempt['probe'],Intent(attempt['product_id'],attempt['side'],attempt['price'],attempt['qty']))
            except (ProbeBlocked,ValueError,KeyError) as exc:
                self.journal.write('recovery_retry_blocked',dict(client_ref=attempt['client_ref'],reason=str(exc)))
                return
            finally:
                self._recovery_exclude_ref = None
            request = self._request_payload(attempt)
            attempt['recovery_retries'] = attempt.get('recovery_retries',0)+1
            attempt['last_recovery_submit_ms'] = now_ms
            attempt.pop('absence_first_ms',None)
            # 可能原请求未提交，此次才计日额度；保守记账，不退回未知/拒单额度。
            self.gate.daily_orders_used += 1
            self.state.data['daily_used'][str(self.trade_date)] = self.gate.daily_orders_used
            self.state.save()
            self.retry_on_throttle = 0
            self._authorized = (threading.get_ident(),'/addOrder',request)
            try:
                result = OrderManager.add_order(self,request['productId'],request['side'],request['ordType'],
                    request['price'],request['orderQty'],request['timeInForce'],
                    client_ref=request['clientRef'],note=request['note'])
                self._record_order_response(attempt,result,is_retry=True)
                if self.last_code in (14,429):
                    self._recovery_next_ms = max(self._recovery_next_ms,now_ms+int((self.retry_after or 1)*1000))
                self.journal.write('recovery_same_ref_retry',dict(client_ref=attempt['client_ref'],
                    result=result,code=self.last_code,error=self.last_error))
            except ProbeBlocked as exc:
                # 这次未发送，不能推出第一次也未发送，继续保留原未知占用。
                self.journal.write('recovery_retry_deferred',dict(client_ref=attempt['client_ref'],reason=str(exc)))
            finally:
                self._authorized = None
                self.state.save()

    def recover_unknown(self, now_ms, stopping=False):
        from .probe_execution import ProbeBlocked
        if self.budget is None or now_ms < getattr(self,'_recovery_next_ms',0):
            return
        pending = [a for a in self.state.data['attempts'].values()
                   if a['status']=='unknown' and now_ms-a['sent_ms'] >= RECOVERY_INTERVAL_MS]
        if not pending:
            self._recovery_scan = None
            return
        self._recovery_next_ms = now_ms+RECOVERY_INTERVAL_MS
        scan = getattr(self,'_recovery_scan',None)
        if scan is None:
            date = min(pending,key=lambda a:a.get('recovery_checked_ms',0))['trade_date']
            scan = dict(date=date,page=0,refs={a['client_ref'] for a in pending if a['trade_date']==date},found=set())
            self._recovery_scan = scan
        try:
            page = self.query_orders(date=scan['date'],page=scan['page'])
        except ProbeBlocked:
            return
        targets = [a for a in self.state.data['attempts'].values() if a['client_ref'] in scan['refs']]
        if page is None or not isinstance(page.get('items'),list) or type(page.get('has_more')) is not bool:
            self.journal.write('recovery_query_failed',dict(date=scan['date'],page=scan['page'],error=self.last_error))
            self._recovery_scan = None
            self._recovery_next_ms = now_ms+max(RECOVERY_INTERVAL_MS,int((self.retry_after or 0)*1000))
            for a in targets:
                a['recovery_checked_ms'] = now_ms
            self.state.save()
            return
        self.journal.write('recovery_orders_page',dict(date=scan['date'],page=scan['page'],data=page))
        for item in page['items']:
            raw = item.get('raw') or {}
            for a in targets:
                matches = raw.get('clientRef') == a['client_ref'] or (a.get('order_id') and str(raw.get('orderId')) == a['order_id'])
                if matches and raw.get('orderId') and raw.get('tradeDate',scan['date']) == a['trade_date']:
                    scan['found'].add(a['client_ref'])
                    self.on_order(raw)
        if page['has_more']:
            scan['page'] += 1
            return  # 每轮最多一页，让出主循环执行撤单。
        self._recovery_scan = None
        for a in targets:
            a['recovery_checked_ms'] = now_ms
            if a['status'] != 'unknown' or a['client_ref'] in scan['found']:
                continue
            first = a.setdefault('absence_first_ms',now_ms)
            # 不把空一页、查询失败或 SDK 的本地 X 单独当成未提交证明。
            if (now_ms-first >= self.not_found_confirm_seconds*1000
                    and now_ms-max(a['sent_ms'],a.get('last_recovery_submit_ms',0)) >= self.processing_expire_seconds*1000):
                a['status'] = 'not_found'
                a['cancel_required'] = True
                a['absence_confirmed_ms'] = now_ms
                if a.get('order_id'):
                    self.portfolio.orders.pop(a['order_id'],None)
                self.journal.write('recovery_not_found',dict(client_ref=a['client_ref'],
                    first_complete_absence_ms=first,second_complete_absence_ms=now_ms,
                    note='按 SDK 失踪规则释放占用，非撤单确认；迟到回报仍须恢复并撤单'))
            elif not stopping:
                self._retry_unknown(a,now_ms)
        self.state.save()
