"""账户级探针状态。先落盘 clientRef 再发送，重启不重放下单。"""
import fcntl
import json
import math
import os
from pathlib import Path


class ProbeState:
    def __init__(self, path: Path, account_id: int):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 探针和正式策略在同一数据目录共用账户锁，不能各自持仓后互相漏账。
        self.lock = (path.parent / f'account-{account_id}.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise RuntimeError('此账户的探针状态已被另一进程使用') from exc
        try:
            self.data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
                account_id=account_id, baselines={}, attempts={}, trades={}, observations={},
                daily_used={}, limits={}, skipped={})
            if self.data['account_id'] != account_id:
                raise ValueError('探针状态文件属于另一账户')
        except Exception:
            self.close()
            raise

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def baseline(self, trade_date: int, supplied):
        key = str(trade_date)
        old = self.data['baselines'].get(key)
        if supplied is not None:
            if not math.isfinite(supplied) or supplied <= 0:
                raise ValueError('日基准净值必须为正数')
            if old is not None and abs(old-supplied) > .01:
                raise ValueError('日基准与本账户当日已保存值不同，不能用重启重置亏损线')
            self.data['baselines'][key] = supplied
            self.save()
        return self.data['baselines'].get(key)

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()
