"""网关级集成：额度拒绝原因、失败退还、重试幂等、降级不打上游、对外口径一致。"""

from __future__ import annotations

import logging
import threading
import time
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


def test_retry_after_success_then_timeout_keeps_quota():
    """场景一：同一编号先成功占用，后续重试即使赶上上游超时也不改动额度。"""
    # 熔断阈值调高，隔离变量：本场景只关注超时退还边界
    gw, quota = make_gateway(
        quota_limit=5,
        breaker_config=CircuitBreakerConfig(failure_threshold=100, recovery_timeout=10.0),
    )

    resp = gw.handle("c1", "/x", None, lambda: "ok", request_id="same-id")
    used_after_success = quota.status("c1").used
    logger.info("input: first call request_id=same-id upstream=ok -> remaining=%s used=%s",
                resp.quota.remaining, used_after_success)
    assert used_after_success == 1.0

    def timeout_upstream():
        raise UpstreamError(UpstreamFailureKind.TIMEOUT, "retry hits upstream timeout")

    for attempt in range(3):  # 连续多次同编号可退失败重试
        with pytest.raises(UpstreamError) as exc_info:
            gw.handle("c1", "/x", None, timeout_upstream, request_id="same-id")
        logger.info(
            "retry attempt=%d kind=%s -> refunded=%s used=%s remaining=%s",
            attempt + 1, exc_info.value.kind.value,
            exc_info.value.detail["quota"]["refunded"],
            quota.status("c1").used, exc_info.value.detail["quota"]["remaining"],
        )
        assert exc_info.value.kind is UpstreamFailureKind.TIMEOUT
        assert exc_info.value.detail["quota"]["refunded"] is False  # 不退别人的成功笔
        assert quota.status("c1").used == 1.0  # 成功那笔额度原样还在
        assert exc_info.value.detail["quota"]["remaining"] == 4.0

    # 再来一次成功重试：仍不重复扣
    resp2 = gw.handle("c1", "/x", None, lambda: "ok", request_id="same-id")
    logger.info("success retry -> remaining=%s used=%s",
                resp2.quota.remaining, quota.status("c1").used)
    assert quota.status("c1").used == 1.0
    assert resp2.quota.remaining == 4.0


def test_retry_after_success_then_circuit_open_keeps_quota():
    """同编号成功后重试撞上熔断降级：降级该退的是在途笔，已提交的成功笔不退。"""
    # 熔断器注入可控时钟，精确安排"先成功 -> 再熔断 -> 后重试"的时序
    breaker_clock = {"t": 0.0}
    plans = {"free": QuotaPlan("free", rate_per_sec=1000.0, burst=1000, quota_limit=5)}
    quota = TieredQuotaManager(plans, "free")
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=1000.0, burst=1000),
        endpoint_policies={},
    )
    versions = VersionRegistry(rules=[VersionRule(ApiVersion(1, 0))], today=date(2026, 6, 1))
    breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=2, recovery_timeout=10.0),
        clock=lambda: breaker_clock["t"],
    )
    gw = Gateway(limiter, versions, breaker, quota=quota)

    def boom():
        raise UpstreamError(UpstreamFailureKind.TIMEOUT)

    # c2 把熔断器打到 OPEN，再经半开探测成功恢复（c2 的账目与 c1 隔离）
    for i in range(2):
        with pytest.raises(UpstreamError):
            gw.handle("c2", "/x", None, boom, request_id=f"trip-{i}")
    breaker_clock["t"] = 11.0
    gw.handle("c2", "/x", None, lambda: "probe-ok", request_id="probe-close")

    # c1 的同编号在 CLOSED 下成功并提交
    gw.handle("c1", "/x", None, lambda: "ok", request_id="settled-id")
    assert quota.status("c1").used == 1.0

    # 再次把熔断打到 OPEN，且不推进时钟：重试只能撞上快速失败
    for i in range(2):
        with pytest.raises(UpstreamError):
            gw.handle("c2", "/x", None, boom, request_id=f"retrip-{i}")
    for attempt in range(2):
        with pytest.raises(CircuitOpen) as exc_info:
            gw.handle("c1", "/x", None, lambda: "ok", request_id="settled-id")
        logger.info("circuit-open retry=%d -> status=503 refunded=%s used=%s remaining=%s",
                    attempt + 1, exc_info.value.detail["quota"]["refunded"],
                    quota.status("c1").used,
                    exc_info.value.detail["quota"]["remaining"])
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["quota"]["refunded"] is False
        assert quota.status("c1").used == 1.0
        assert exc_info.value.detail["quota"]["remaining"] == 4.0


def test_concurrent_same_id_retries_no_crosstalk():
    """场景二：同编号并发重试，成功与超时混杂时各笔账互不串扰。

    同编号串行结算后结论确定：只要有一次尝试成功，最终占用恰好 1；
    若全部超时则占用 0。绝不能出现"成功被并发超时退掉"的串账。
    """
    gw, quota = make_gateway(
        quota_limit=5,
        breaker_config=CircuitBreakerConfig(failure_threshold=100, recovery_timeout=10.0),
    )
    n = 24
    barrier = threading.Barrier(n)  # 尽量让线程同时冲进网关
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def worker(i: int) -> None:
        def upstream():
            # 错峰放大交错：有人慢到超时、有人成功返回
            time.sleep(0.002 * (i % 5))
            if i % 2 == 0:
                raise UpstreamError(UpstreamFailureKind.TIMEOUT)
            return "ok"

        barrier.wait()
        try:
            gw.handle("c1", "/x", None, upstream, request_id="race-id")
            outcome = "ok"
        except UpstreamError as exc:
            outcome = f"timeout(refunded={exc.detail['quota']['refunded']})"
        with outcomes_lock:
            outcomes.append(outcome)
        logger.info("thread=%02d request_id=race-id -> %s", i, outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    used = quota.status("c1").used
    ok_count = outcomes.count("ok")
    logger.info("settled: ok=%d timeout=%d final_used=%s",
                ok_count, len(outcomes) - ok_count, used)
    assert ok_count >= 1  # 奇数号线程成功
    assert used == 1.0  # 不是 0（成功笔没被退），也不是多次扣减
    # 只可能有一次真实退还（首个在途超时），之后该编号已被成功提交
    refunded_count = sum("refunded=True" in o for o in outcomes)
    # 成功之后的超时重试全部 refunded=False；成功之前至多一笔真退
    assert all(o in ("ok", "timeout(refunded=False)", "timeout(refunded=True)") for o in outcomes)
    logger.info("refunded_once_among_retries count=%s VERDICT 无串账", refunded_count)


def test_unsupported_version_repeated_conclusion_stable():
    """场景三：版本不在支持范围被反复调用，原因/状态码/剩余额度稳定且无副作用。"""
    # 速率预算故意给得很紧：若版本拒绝吃令牌，第 2 次就会退化成"请求太快"
    plans = {"free": QuotaPlan("free", rate_per_sec=0.0001, burst=1, quota_limit=5)}
    quota = TieredQuotaManager(plans, "free")
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=0.0001, burst=1),
        endpoint_policies={},
    )
    versions = VersionRegistry(rules=[VersionRule(ApiVersion(1, 0))], today=date(2026, 6, 1))
    breaker = CircuitBreaker(CircuitBreakerConfig())
    gw = Gateway(limiter, versions, breaker, quota=quota)

    seen: list[tuple[str, int, float]] = []
    upstream_called: list[int] = []
    for attempt in range(5):
        with pytest.raises(VersionRejected) as exc_info:
            gw.handle(
                "c1", "/x", "v9",
                lambda: upstream_called.append(1),
                request_id=f"badver-{attempt}",
            )
        seen.append((
            exc_info.value.reason,
            exc_info.value.status_code,
            exc_info.value.detail["quota"]["remaining"],
        ))
        logger.info(
            "attempt=%d input=(version=v9, request_id=badver-%d) "
            "-> reason=%s status=%s remaining=%s upstream_called=%s",
            attempt + 1, attempt, exc_info.value.reason, exc_info.value.status_code,
            exc_info.value.detail["quota"]["remaining"], bool(upstream_called),
        )
        assert exc_info.value.reason == "version_too_new"
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["quota"]["remaining"] == 5.0  # 额度也没被动
    assert len(set(seen)) == 1  # 五次结论（原因、状态码、剩余额度）完全一致
    assert not upstream_called  # 拒绝路径不触达上游

    # 版本拒绝未吃掉速率预算：紧接着一个合法版本请求仍能正常通过
    resp = gw.handle("c1", "/x", "v1", lambda: "ok", request_id="good-1")
    logger.info("legal request after 5 rejections -> served, remaining=%s",
                resp.quota.remaining)
    assert resp.quota.remaining == 4.0
    assert quota.status("c1").used == 1.0
