"""HTTP 层：统一响应信封、拒绝原因区分、并发幂等、降级不触达上游。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient

logger = logging.getLogger("test.http")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个用例独立状态文件，默认上游成功
    monkeypatch.setenv("GATEWAY_STATE_PATH", str(tmp_path / "http_state.json"))
    monkeypatch.setenv("UPSTREAM_MODE", "ok")
    import importlib
    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, main


def _assert_quota_block(payload: dict) -> None:
    q = payload["quota"]
    assert q is not None
    for key in ("plan", "rate_remaining", "rate_capacity", "quota_limit",
                "quota_used", "quota_remaining", "quota_reset_in_sec"):
        assert key in q, f"missing {key}"


def test_success_envelope_is_stable_and_complete(client):
    c, _ = client
    r = c.get("/api/data", headers={"X-Client-Id": "alice", "X-Request-Id": "r1"})
    assert r.status_code == 200
    body = r.json()
    logger.info("success body keys=%s", sorted(body.keys()))
    assert body["status"] == "ok"
    assert set(["body", "version", "cost", "replayed", "quota"]).issubset(body)
    _assert_quota_block(body)


def test_idempotent_retry_replays_and_does_not_double_charge(client):
    c, main = client
    h = {"X-Client-Id": "alice", "X-Request-Id": "dup-1"}
    r1 = c.get("/api/data", headers=h)
    r2 = c.get("/api/data", headers=h)
    assert r1.status_code == r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    logger.info("retry #1 used=%s #2 used=%s replayed=%s",
                j1["quota"]["quota_used"], j2["quota"]["quota_used"], j2["replayed"])
    assert j1["replayed"] is False and j2["replayed"] is True
    assert j1["quota"]["quota_used"] == j2["quota"]["quota_used"] == 1.0


def test_quota_endpoint_reports_remaining_and_plan(client):
    c, _ = client
    c.get("/api/data", headers={"X-Client-Id": "bob"})
    r = c.get("/quota", headers={"X-Client-Id": "bob"})
    assert r.status_code == 200
    q = r.json()["quota"]
    logger.info("quota view=%s", q)
    assert q["plan"] == "basic" and q["quota_used"] == 1.0
    assert q["quota_remaining"] == q["quota_limit"] - 1


def test_rate_rejection_envelope_has_429_and_retry_after(client):
    c, main = client
    main.accounting._plans.set_override(
        "fast", main.Plan("fast", rate_per_sec=0.0001, burst=1,
                          quota_limit=1000, quota_period_sec=86400.0,
                          reset_mode=main.QuotaResetMode.DAILY),
    )
    c.get("/api/data", headers={"X-Client-Id": "fast"})
    r = c.get("/api/data", headers={"X-Client-Id": "fast"})
    j = r.json()
    logger.info("rate rejected status=%s reason=%s retry_after=%s",
                r.status_code, j["reason"], j["retry_after"])
    assert r.status_code == 429 and j["reason"] == "client_rate_exceeded"
    assert j["retry_after"] > 0 and r.headers.get("Retry-After")
    _assert_quota_block(j)


def test_quota_exhaustion_is_distinguishable_from_rate(client):
    c, main = client
    # 小总量、大突发：耗尽总量时报 quota_exhausted 而非 rate
    main.accounting._plans.set_override(
        "tiny", main.Plan("tiny", rate_per_sec=1000.0, burst=1000,
                          quota_limit=2, quota_period_sec=86400.0,
                          reset_mode=main.QuotaResetMode.DAILY),
    )
    c.get("/api/data", headers={"X-Client-Id": "tiny"})
    c.get("/api/data", headers={"X-Client-Id": "tiny"})
    r = c.get("/api/data", headers={"X-Client-Id": "tiny"})
    j = r.json()
    logger.info("quota rejected status=%s reason=%s detail=%s",
                r.status_code, j["reason"], j["quota"]["quota_remaining"])
    assert r.status_code == 429 and j["reason"] == "quota_exhausted"
    assert j["quota"]["quota_remaining"] == 0


def test_malformed_declared_cost_returns_400_without_charging(client):
    c, _ = client
    r = c.get("/api/heavy", headers={"X-Client-Id": "alice", "X-Declared-Cost": "??"})
    j = r.json()
    assert r.status_code == 400 and j["reason"] == "declared_cost_malformed"
    q = c.get("/quota", headers={"X-Client-Id": "alice"}).json()["quota"]
    logger.info("after malformed declaration quota_used=%s", q["quota_used"])
    assert q["quota_used"] == 0.0


def test_version_rejection_uses_consistent_envelope(client):
    c, _ = client
    r = c.get("/api/data", headers={"X-Client-Id": "alice", "X-Api-Version": "v9"})
    j = r.json()
    logger.info("version reject status=%s reason=%s", r.status_code, j["reason"])
    assert r.status_code == 400 and j["reason"] == "version_too_new"
    assert j["status"] == "rejected" and "retry_after" in j
    _assert_quota_block(j)


def test_degraded_response_is_503_and_upstream_not_called(client, monkeypatch):
    c, main = client
    monkeypatch.setenv("UPSTREAM_MODE", "timeout")
    calls = []
    # 用 monkeypatch 直接观测：call_upstream 在 OPEN 后不应再被调用
    original = main.call_upstream

    def tracked():
        calls.append(1)
        return original()

    main.call_upstream = tracked
    for i in range(3):
        r = c.get("/api/data", headers={"X-Client-Id": "downtime", "X-Request-Id": f"x{i}"})
        assert r.status_code == 502 and r.json()["reason"] == "upstream_timeout"
    before = len(calls)
    r = c.get("/api/data", headers={"X-Client-Id": "downtime", "X-Request-Id": "x-open"})
    j = r.json()
    logger.info("degraded status=%s reason=%s retry_after=%s calls_delta=%d",
                r.status_code, j["reason"], j["retry_after"], len(calls) - before)
    assert r.status_code == 503 and j["reason"] == "circuit_open"
    assert j["retry_after"] > 0 and r.headers.get("Retry-After")
    assert len(calls) == before  # OPEN 期间不再触达上游
    assert j["quota"]["quota_used"] == 0.0  # 超时与降级都退还


def test_concurrent_duplicate_retries_never_double_charge(client):
    c, main = client
    results: list[int] = []
    lock = threading.Lock()

    def one():
        r = c.get("/api/data", headers={"X-Client-Id": "race", "X-Request-Id": "same-id"})
        with lock:
            results.append(r.status_code)
        return r.status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda _: one(), range(12)))

    logger.info("concurrent same-id statuses=%s", sorted(set(statuses)))
    assert statuses.count(200) >= 1
    # 只有一个真正执行（200 且 replayed=False）；其余 200 重放或 409 在途冲突，
    # 无论哪种，总量只能被扣一次
    used = c.get("/quota", headers={"X-Client-Id": "race"}).json()["quota"]["quota_used"]
    logger.info("final used after concurrent retries=%s", used)
    assert used == 1.0
