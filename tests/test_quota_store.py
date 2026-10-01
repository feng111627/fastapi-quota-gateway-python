"""快照存储测试：原子落盘、缺失/损坏容忍、往返一致。"""

import logging
import os

from app.quota.store import QuotaStore

logger = logging.getLogger("tests.store")


def test_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = QuotaStore(str(path))
    snapshot = {"saved_at": 1.0,
                "buckets": {"client:free": {"tokens": 1.5, "last_refill": 42.0}}}
    store.save(snapshot)
    loaded = store.load()
    logger.info("输入快照=%s 读取=%s", snapshot, loaded)
    assert loaded == snapshot
    assert not any(f.startswith(tempfile_prefix()) for f in os.listdir(path.parent))


def tempfile_prefix() -> str:
    return ".quota-"


def test_load_missing_returns_empty(tmp_path) -> None:
    logger.info("输入 不存在路径，期望空字典")
    assert QuotaStore(str(tmp_path / "x.json")).load() == {}


def test_load_corrupt_returns_empty(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not-json{", encoding="utf-8")
    result = QuotaStore(str(path)).load()
    logger.info("输入 损坏文件，期望空字典，实际=%s", result)
    assert result == {}
