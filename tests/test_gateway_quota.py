"""网关级集成：额度拒绝原因、失败退还、重试幂等、降级不打上游、对外口径一致。"""

from __future__ import annotations

import logging
import threading
from datetime import date

import pytest

from gateway import (
    ApiVersion,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpen,
    CostPolicy,
    Gateway,
    MultiLevelRateLimiter,
    QuotaPlan,
    RateLimitExceeded,
    RateLimitPolicy,
    RateLimitReason,
    TieredQuotaManager,
    UpstreamError,
    UpstreamFailureKind,
    VersionRejected,
    VersionRegistry,
    VersionRule,
)

logger = logging.getLogger("test.gateway.quota")


def make_gateway(*, quota_limit: int = 5, breaker_config: CircuitBreakerConfig | None = None):
    """高速率上限，把配额维度隔离出来测。"""
    plans = {"free": QuotaPlan("free", rate_per_sec=1000.0, burst=1000, quota_limit=quota_limit)}
    quota = TieredQuotaManager(plans, "free")
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=1000.0, burst=1000),
        endpoint_policies={},
        client_policy_resolver=lambda cid: RateLimitPolicy(
            rate_per_sec=quota.plan_for(cid).rate_per_sec,
            burst=quota.plan_for(cid).burst,
        ),
    )
    versions = VersionRegistry(rules=[VersionRule(ApiVersion(1, 0))], today=date(2026, 6, 1))
    breaker = CircuitBreaker(breaker_config or CircuitBreakerConfig(
        failure_threshold=2, recovery_timeout=10.0))
    costs = CostPolicy({"/heavy": 2.0}, min_declared=1.0, max_declared=4.0)
    gw = Gateway(limiter, versions, breaker, quota=quota, costs=costs)
    return gw, quota


def test_quota_exhausted_reason_distinct_from_rate_limit():
    gw, quota = make_gateway(quota_limit=2)
    for i in range(2):
        gw.handle("c1", "/x", None, lambda: "ok", request_id=f"r{i}")
    for attempt in range(3):  # 反复拦截，结论可复现
        with pytest.raises(RateLimitExceeded) as exc_info:
            gw.handle("c1", "/x", None, lambda: "ok", request_id=f"rx{attempt}")
        logger.info("quota exhausted attempt=%d -> reason=%s retry_after=%s quota=%s",
                    attempt, exc_info.value.reason, exc_info.value.retry_after,
                    exc_info.value.detail.get("quota"))
        assert exc_info.value.limit_reason is RateLimitReason.QUOTA_EXHAUSTED
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after > 0
        assert exc_info.value.detail["quota"]["remaining"] == 0.0
    # 速率维度未被额度拒绝消耗：速率令牌仍然充足（reason 不会退化成 rate_exceeded）
    assert quota.status("c1").remaining == 0.0


def test_rate_limit_reason_still_distinct():
    """速率耗尽与额度用完是两种不同的拒绝。"""
    plans = {"free": QuotaPlan("free", rate_per_sec=0.0001, burst=1, quota_limit=100)}
    quota = TieredQuotaManager(plans, "free")
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=0.0001, burst=1),
        endpoint_policies={},
        client_policy_resolver=lambda cid: RateLimitPolicy(
            rate_per_sec=quota.plan_for(cid).rate_per_sec,
            burst=quota.plan_for(cid).burst,
        ),
    )
    versions = VersionRegistry(rules=[VersionRule(ApiVersion(1, 0))], today=date(2026, 6, 1))
    breaker = CircuitBreaker(CircuitBreakerConfig())
    gw = Gateway(limiter, versions, breaker, quota=quota)
    gw.handle("c1", "/x", None, lambda: "ok", request_id="r0")
    with pytest.raises(RateLimitExceeded) as exc_info:
        gw.handle("c1", "/x", None, lambda: "ok", request_id="r1")
    logger.info("rate limited -> reason=%s quota=%s",
                exc_info.value.reason, exc_info.value.detail.get("quota"))
    assert exc_info.value.limit_reason is RateLimitReason.CLIENT_RATE_EXCEEDED
    assert exc_info.value.detail["quota"]["remaining"] == 99.0  # 速率拒绝不扣额度


def test_declared_cost_clamped_and_surfaced():
    gw, quota = make_gateway(quota_limit=100)
    resp = gw.handle("c1", "/heavy", None, lambda: "ok",
                     declared_cost=999.0, request_id="r1")
    logger.info("declared=999 -> applied=%s clamped=%s remaining=%s",
                resp.cost.cost, resp.cost.clamped, resp.quota.remaining)
    assert resp.cost.clamped
    assert resp.cost.cost == 8.0  # weight 2.0 × 夹到上限 4.0
    assert resp.quota.remaining == 92.0
    assert resp.quota.plan == "free"
    assert resp.quota.limit == 100


def test_upstream_timeout_and_connection_refund():
    gw, quota = make_gateway(quota_limit=5)
    for i, kind in enumerate([UpstreamFailureKind.TIMEOUT, UpstreamFailureKind.CONNECTION]):
        def boom(k=kind):
            raise UpstreamError(k)
        with pytest.raises(UpstreamError) as exc_info:
            gw.handle("c1", "/x", None, boom, request_id=f"fail-{i}")
        logger.info("kind=%s -> refunded=%s remaining=%s",
                    kind.value, exc_info.value.detail["quota"]["refunded"],
                    quota.status("c1").remaining)
        assert exc_info.value.detail["quota"]["refunded"] is True
    assert quota.status("c1").remaining == 5.0  # 两次都退回来了


def test_business_error_not_refunded():
    gw, quota = make_gateway(quota_limit=5)

    def boom():
        raise UpstreamError(UpstreamFailureKind.BUSINESS)

    with pytest.raises(UpstreamError) as exc_info:
        gw.handle("c1", "/x", None, boom, request_id="biz-1")
    logger.info("business error -> refunded=%s remaining=%s",
                exc_info.value.detail["quota"]["refunded"], quota.status("c1").remaining)
    assert exc_info.value.detail["quota"]["refunded"] is False
    assert quota.status("c1").remaining == 4.0  # 业务错误照常计费


def test_circuit_open_refunds_and_never_hits_upstream():
    gw, quota = make_gateway(quota_limit=5)

    def boom():
        raise UpstreamError(UpstreamFailureKind.TIMEOUT)

    for i in range(2):  # 打满失败阈值，熔断
        with pytest.raises(UpstreamError):
            gw.handle("c1", "/x", None, boom, request_id=f"t{i}")
    assert quota.status("c1").remaining == 5.0  # 超时已全退

    called = []
    with pytest.raises(CircuitOpen) as exc_info:
        gw.handle("c1", "/x", None, lambda: called.append(1), request_id="degraded")
    logger.info("circuit open -> upstream_called=%s retry_after=%s refunded=%s remaining=%s",
                bool(called), exc_info.value.retry_after,
                exc_info.value.detail["quota"]["refunded"], quota.status("c1").remaining)
    assert not called  # 降级不触达上游
    assert exc_info.value.retry_after > 0  # 恢复等待时间对外可见
    assert exc_info.value.detail["quota"]["refunded"] is True
    assert quota.status("c1").remaining == 5.0  # 降级白扣的也退了


def test_retry_same_request_id_charged_once():
    gw, quota = make_gateway(quota_limit=10)
    for attempt in range(3):
        resp = gw.handle("c1", "/x", None, lambda: "ok", request_id="same-id")
        logger.info("attempt=%d -> remaining=%s", attempt, resp.quota.remaining)
    assert quota.status("c1").used == 1.0  # 三次重试只扣一次


def test_concurrent_gateway_no_over_issue():
    """并发请求全部成功时：通过数恰好等于额度上限，其余按 quota_exhausted 拒绝。"""
    gw, quota = make_gateway(quota_limit=20)
    n = 40
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            gw.handle("c1", "/x", None, lambda: "ok", request_id=f"req-{i}")
            outcome = "ok"
        except RateLimitExceeded as exc:
            outcome = f"rejected:{exc.reason}"
        with lock:
            outcomes.append(outcome)
        logger.info("req=%d -> %s", i, outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = outcomes.count("ok")
    logger.info("ok=%d rejected=%d used=%s", ok, n - ok, quota.status("c1").used)
    assert ok == 20  # 不超发不少发
    assert all(o in ("ok", "rejected:quota_exhausted") for o in outcomes)
    assert quota.status("c1").used == 20.0


def test_concurrent_gateway_charge_and_refund():
    """并发成功/失败混合：不超发，失败的全额退还，最终账目 = 成功数。"""
    gw, quota = make_gateway(quota_limit=20)
    n = 40
    outcomes: list[str] = []
    refund_flags: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        def upstream():
            if i % 2 == 0:
                raise UpstreamError(UpstreamFailureKind.TIMEOUT)
            return "ok"
        try:
            gw.handle("c1", "/x", None, upstream, request_id=f"req-{i}")
            outcome = "ok"
        except UpstreamError as exc:
            outcome = "refunded"
            with lock:
                refund_flags.append(exc.detail["quota"]["refunded"])
        except RateLimitExceeded:
            outcome = "rejected"
        with lock:
            outcomes.append(outcome)
        logger.info("req=%d -> %s", i, outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = outcomes.count("ok")
    refunded = outcomes.count("refunded")
    logger.info("ok=%d refunded=%d rejected=%d used=%s",
                ok, refunded, outcomes.count("rejected"), quota.status("c1").used)
    assert ok <= 20  # 任一时刻在账额度不超上限
    assert all(refund_flags)  # 每次超时都退还了
    assert quota.status("c1").used == float(ok)  # 最终只有成功的占用额度


def test_error_surface_consistent_across_rejections():
    """限流/版本/降级各类拒绝都带统一的额度视图。"""
    gw, quota = make_gateway(quota_limit=1)
    gw.handle("c1", "/x", None, lambda: "ok", request_id="use-it")

    with pytest.raises(RateLimitExceeded) as quota_exc:
        gw.handle("c1", "/x", None, lambda: "ok", request_id="q1")
    with pytest.raises(VersionRejected) as ver_exc:
        gw.handle("c2", "/x", "v9", lambda: "ok", request_id="v1")

    for name, exc in [("quota", quota_exc.value), ("version", ver_exc.value)]:
        q = exc.detail.get("quota")
        logger.info("%s rejection -> status=%s reason=%s quota=%s",
                    name, exc.status_code, exc.reason, q)
        assert q is not None
        assert {"plan", "limit", "remaining", "reset_at"} <= set(q)


def test_success_response_carries_quota_view():
    gw, quota = make_gateway(quota_limit=10)
    resp = gw.handle("c1", "/x", None, lambda: "ok", request_id="r1")
    logger.info("success -> plan=%s limit=%s remaining=%s reset_at=%s cost=%s",
                resp.quota.plan, resp.quota.limit, resp.quota.remaining,
                resp.quota.reset_at, resp.quota.cost)
    assert resp.quota.remaining == 9.0
    assert resp.quota.reset_at > 0
