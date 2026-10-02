"""分档计费网关编排：退还规则、幂等重放、判定顺序与结论可复现。"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from gateway import (
    AccountingGate,
    ApiVersion,
    BillingRejected,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpen,
    CostResolver,
    EndpointCostPolicy,
    Plan,
    PlanRegistry,
    QuotaGateway,
    RateLimitExceeded,
    UpstreamError,
    UpstreamFailureKind,
    VersionRejected,
    VersionRegistry,
    VersionRule,
)

logger = logging.getLogger("test.quota_gateway")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gateway(
    *,
    rate: float = 1000.0,
    burst: int = 100,
    quota: int = 100,
    period: float = 100.0,
    threshold: int = 2,
    recovery: float = 30.0,
) -> tuple[QuotaGateway, FakeClock]:
    clk = FakeClock()
    plan = Plan("basic", rate, burst, quota, period)
    gate = AccountingGate(PlanRegistry(plan), clock=clk)
    versions = VersionRegistry(
        [VersionRule(ApiVersion(1, 0), deprecated_since=date(2026, 1, 1),
                     sunset_on=date(2026, 12, 31)),
         VersionRule(ApiVersion(2, 0))],
        today=date(2026, 6, 1),
    )
    breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=threshold, recovery_timeout=recovery),
        clock=clk,
    )
    costs = CostResolver({"/heavy": EndpointCostPolicy(weight=3.0, min_declared=1.0, max_declared=5.0)})
    return QuotaGateway(gate, versions, breaker, costs), clk


def fail(kind: UpstreamFailureKind):
    def _up():
        raise UpstreamError(kind, f"simulated {kind.value}")
    return _up


def test_success_charges_weight_and_returns_quota_view():
    gw, _ = make_gateway()
    resp = gw.handle("c", "/heavy", None, lambda: {"ok": True})
    env = resp.to_envelope()
    logger.info("success envelope=%s", env)
    assert resp.cost == 3.0 and resp.replayed is False
    assert env["status"] == "ok" and env["quota"]["quota_used"] == 3.0


def test_timeout_refunds_and_replay_does_not_touch_upstream():
    gw, _ = make_gateway(quota=10)
    upstream_calls: list[int] = []

    def up():
        upstream_calls.append(1)
        raise UpstreamError(UpstreamFailureKind.TIMEOUT, "slow")

    with pytest.raises(UpstreamError) as exc:
        gw.handle("c", "/heavy", None, up, request_id="r1", declared_cost=4)
    assert exc.value.kind is UpstreamFailureKind.TIMEOUT
    logger.info("first timeout -> quota_used=%s upstream_calls=%d",
                gw.gate.view("c").quota.used, len(upstream_calls))
    assert gw.gate.view("c").quota.used == 0.0

    # 同编号重试：重放失败结论，不触达上游，不重复扣/退
    with pytest.raises(UpstreamError) as exc2:
        gw.handle("c", "/heavy", None, lambda: upstream_calls.append(1),
                  request_id="r1", declared_cost=4)
    logger.info("replay -> quota_used=%s upstream_calls=%d",
                gw.gate.view("c").quota.used, len(upstream_calls))
    assert exc2.value.kind is UpstreamFailureKind.TIMEOUT
    assert len(upstream_calls) == 1
    assert gw.gate.view("c").quota.used == 0.0


def test_connection_failure_refunds_like_timeout():
    gw, _ = make_gateway(quota=5)
    with pytest.raises(UpstreamError) as exc:
        gw.handle("c", "/heavy", None, fail(UpstreamFailureKind.CONNECTION),
                  request_id="r", declared_cost=2)
    assert exc.value.kind is UpstreamFailureKind.CONNECTION
    assert gw.gate.view("c").quota.used == 0.0
    logger.info("connection failure refunded -> used=0")


def test_business_failure_is_charged_and_replayed_as_failure():
    gw, _ = make_gateway(quota=10)
    calls = []

    def up():
        calls.append(1)
        raise UpstreamError(UpstreamFailureKind.BUSINESS, "500")

    for _ in range(2):
        with pytest.raises(UpstreamError) as exc:
            gw.handle("c", "/heavy", None, up, request_id="rb", declared_cost=2)
        assert exc.value.kind is UpstreamFailureKind.BUSINESS
    logger.info("business failure twice: calls=%d used=%s",
                len(calls), gw.gate.view("c").quota.used)
    assert len(calls) == 1  # 第二次是重放
    assert gw.gate.view("c").quota.used == 2.0  # 不退


def test_circuit_open_refunds_and_never_calls_upstream():
    gw, _ = make_gateway(quota=10, threshold=2)
    for i in range(2):
        with pytest.raises(UpstreamError):
            gw.handle("c", "/heavy", None, fail(UpstreamFailureKind.TIMEOUT),
                      request_id=f"f{i}", declared_cost=1)
    called = []
    with pytest.raises(CircuitOpen) as exc:
        gw.handle("c", "/heavy", None, lambda: called.append(1),
                  request_id="degraded", declared_cost=2)
    logger.info("circuit open -> retry_after=%s called=%s used=%s",
                exc.value.retry_after, bool(called), gw.gate.view("c").quota.used)
    assert not called and exc.value.retry_after > 0
    assert gw.gate.view("c").quota.used == 0.0  # 降级没拿到数据，退还

    # 同编号立即重试仍是降级（未固化为终态，熔断器状态说了算），上游依旧不触达
    with pytest.raises(CircuitOpen):
        gw.handle("c", "/heavy", None, lambda: called.append(1),
                  request_id="degraded", declared_cost=2)
    assert not called


def test_circuit_recovers_after_timeout_and_half_open_logic_intact():
    gw, clk = make_gateway(quota=10, threshold=1, recovery=10.0)
    with pytest.raises(UpstreamError):
        gw.handle("c", "/heavy", None, fail(UpstreamFailureKind.CONNECTION),
                  request_id="f", declared_cost=1)
    with pytest.raises(CircuitOpen):
        gw.handle("c", "/heavy", None, lambda: "ok", request_id="d")
    clk.advance(10.0)
    resp = gw.handle("c", "/heavy", None, lambda: "recovered", request_id="d")
    logger.info("half-open probe success -> body=%s used=%s",
                resp.body, resp.quota.quota.used)
    assert resp.body == "recovered" and resp.quota.quota.used == 3.0


def test_malformed_cost_rejected_before_any_charge_or_upstream():
    gw, _ = make_gateway()
    called = []
    with pytest.raises(BillingRejected) as exc:
        gw.handle("c", "/heavy", None, lambda: called.append(1), declared_cost="wat")
    assert exc.value.reason == "declared_cost_malformed"
    assert not called and gw.gate.view("c").quota.used == 0.0
    logger.info("malformed cost -> no charge, no upstream call")


def test_version_rejection_rolls_back_and_releases_request_id():
    gw, _ = make_gateway()
    with pytest.raises(VersionRejected):
        gw.handle("c", "/heavy", "v9", lambda: 1, request_id="rv", declared_cost=2)
    assert gw.gate.view("c").quota.used == 0.0
    # request_id 被释放：同一编号可重新发起并成功
    resp = gw.handle("c", "/heavy", None, lambda: "now-ok",
                     request_id="rv", declared_cost=2)
    assert resp.body == "now-ok" and resp.quota.quota.used == 2.0
    logger.info("version rejection rolled back; id reused -> charged 2")


def test_rate_limit_takes_precedence_over_version_for_new_gateway():
    """新网关顺序同样是先计费后版本：额度被拦时版本不再协商（稳定、可复现）。"""
    gw, _ = make_gateway(rate=0.0001, burst=1, quota=100)
    gw.handle("c", "/heavy", None, lambda: 1, declared_cost=1)
    with pytest.raises(RateLimitExceeded) as exc:
        gw.handle("c", "/heavy", "garbage", lambda: 1, declared_cost=1)
    logger.info("rate+bad version -> reason=%s", exc.value.reason)
    assert exc.value.reason == "client_rate_exceeded"


def test_repeated_requests_yield_deterministic_outcomes():
    """同一输入连跑多次，成本、拒绝原因、建议等待完全一致。"""
    gw, _ = make_gateway(rate=0.0001, burst=2, quota=2)
    first = gw.handle("c", "/heavy", None, lambda: 1, declared_cost=2)
    assert first.cost == 2.0
    with pytest.raises(Exception) as e1:
        gw.handle("c", "/heavy", None, lambda: 1, declared_cost=2)
    with pytest.raises(Exception) as e2:
        gw.handle("c", "/heavy", None, lambda: 1, declared_cost=2)
    logger.info("repeat rejects: %s / %s", e1.value.reason, e2.value.reason)
    assert type(e1.value) is type(e2.value)
    assert e1.value.reason == e2.value.reason
    assert getattr(e1.value, "retry_after", None) == getattr(e2.value, "retry_after", None)


def test_deprecated_version_still_carried_through_new_gateway():
    gw, _ = make_gateway()
    resp = gw.handle("c", "/heavy", "v1", lambda: 1)
    logger.info("deprecated version warning=%s", resp.version.warning)
    assert resp.version.warning and "弃用" in resp.version.warning
