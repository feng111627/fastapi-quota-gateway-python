"""接入层编排：判定顺序与错误归类的一致性。"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from gateway import (
    ApiVersion,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpen,
    Gateway,
    MultiLevelRateLimiter,
    RateLimitExceeded,
    RateLimitPolicy,
    RateLimitReason,
    UpstreamError,
    UpstreamFailureKind,
    VersionRejected,
    VersionRegistry,
    VersionRule,
)

logger = logging.getLogger("test.service")


def make_gateway() -> Gateway:
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=0.0001, burst=2),
        endpoint_policies={},
    )
    versions = VersionRegistry(
        rules=[
            VersionRule(ApiVersion(1, 0), deprecated_since=date(2026, 1, 1),
                        sunset_on=date(2026, 12, 31)),
            VersionRule(ApiVersion(2, 0)),
        ],
        today=date(2026, 6, 1),
    )
    breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, recovery_timeout=10.0))
    return Gateway(limiter, versions, breaker)


def test_rate_limit_checked_before_version():
    """配额耗尽时，即使版本非法也先报限流（判定顺序固定）。"""
    gw = make_gateway()
    gw.handle("c1", "/x", None, lambda: "ok")
    gw.handle("c1", "/x", None, lambda: "ok")
    with pytest.raises(RateLimitExceeded) as exc_info:
        gw.handle("c1", "/x", "not-a-version", lambda: "ok")
    logger.info("over-quota + bad version -> reason=%s", exc_info.value.reason)
    assert exc_info.value.limit_reason is RateLimitReason.CLIENT_RATE_EXCEEDED


def test_version_checked_before_upstream():
    """版本非法时上游不被调用，熔断计数不受影响。"""
    gw = make_gateway()
    called = []
    with pytest.raises(VersionRejected):
        gw.handle("c1", "/x", "v9", lambda: called.append(1))
    logger.info("bad version -> upstream_called=%s", bool(called))
    assert not called


def test_deprecated_version_response_carries_warning():
    gw = make_gateway()
    resp = gw.handle("c1", "/x", "v1", lambda: "ok")
    logger.info("deprecated version -> warning=%s", resp.version.warning)
    assert resp.version.warning


def test_upstream_failures_trip_breaker_then_fast_fail():
    gw = make_gateway()

    def boom():
        raise UpstreamError(UpstreamFailureKind.TIMEOUT)

    for i in range(2):
        with pytest.raises(UpstreamError) as exc_info:
            gw.handle(f"c{i}", "/x", None, boom)
        logger.info("upstream failure #%d kind=%s", i + 1, exc_info.value.kind)

    called = []
    with pytest.raises(CircuitOpen):
        gw.handle("c3", "/x", None, lambda: called.append(1))
    logger.info("circuit open -> upstream_called=%s", bool(called))
    assert not called
