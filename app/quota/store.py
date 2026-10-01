"""配额状态持久化：进程重启后按快照恢复计数。

- 落盘采用“临时文件 + os.replace 原子替换”，避免写入中断产生损坏文件；
- 快照结构：{"saved_at": <epoch>, "buckets": {"<level>:<key...>": {tokens,last_refill}}}；
- key 为复合键（如 c:free、a:echo、ca:free|data），用分隔符避免歧义。
"""

from __future__ import annotations

import json
import os
import tempfile


class QuotaStore:
    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # 文件损坏时按冷启动处理，绝不让进程启动失败
            return {}
        if not isinstance(data, dict) or "buckets" not in data:
            return {}
        return data

    def save(self, snapshot: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".quota-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
