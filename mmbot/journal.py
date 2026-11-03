"""JSONL 原始事件记录；服务器未提供时间时保留 null，不能冒充实测。"""
import json
import logging
import re
import threading
import time
from pathlib import Path

KEY_PATTERN = re.compile(r'ak_[A-Za-z0-9_-]{43}')


def redact(text: str, secret='') -> str:
    if secret:
        text = text.replace(secret, '[REDACTED]')
    return KEY_PATTERN.sub('[REDACTED]', text)


class SecretFilter(logging.Filter):
    def __init__(self, secret):
        super().__init__()
        self.secret = secret

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secret)
        record.args = ()
        return True


class Journal:
    def __init__(self, path: Path, secret=''):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a', encoding='utf-8')
        self.secret = secret
        self._lock = threading.Lock()
        self.server_offset_ms = None

    def write(self, kind: str, payload, server_time_ms=None, recv_monotonic=None, recv_wall_ms=None):
        recv_wall_ms = int(time.time()*1000) if recv_wall_ms is None else recv_wall_ms
        record = dict(kind=kind, payload=payload, server_time_ms=server_time_ms,
                      recv_monotonic=time.monotonic() if recv_monotonic is None else recv_monotonic,
                      recv_wall_ms=recv_wall_ms,
                      estimated_server_time_ms=None if self.server_offset_ms is None else recv_wall_ms+self.server_offset_ms)
        line = redact(json.dumps(record, ensure_ascii=False, allow_nan=False), self.secret)
        with self._lock:
            self.file.write(line + '\n')

    def flush(self):
        with self._lock:
            self.file.flush()

    def close(self):
        with self._lock:
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def replay(path: Path):
    """按接收顺序回放，保留原始双时钟；不按未来事件重新排序。"""
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'{path} 第 {number} 行损坏') from exc
