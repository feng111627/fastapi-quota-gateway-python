"""熔断状态转换与失败归类测试。"""

from __future__ import annotations

import logging
import threading

import pytest

from gateway import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpen,
    CircuitState,
    UpstreamError,
    UpstreamFailureKind,
)

logger = logging.getLogger("test.circuitbreaker")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(clock: FakeClock, **cfg) -> CircuitBreaker:
    config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=10.0, **cfg)
    return CircuitBreaker(config, clock=clock)


def fail_with(kind: UpstreamFailureKind):
    def _fn():
        raise UpstreamError(kind, f"simulated {kind.value}")
    return _fn


def test_closed_to_open_after_consecutive_failures():
    clock = FakeClock()
    cb = make_breaker(clock)
    for i in range(3):
        with pytest.raises(UpstreamError) as exc_info:
            cb.call(fail_with(UpstreamFailureKind.TIMEOUT))
        logger.info("failure #%d kind=%s state=%s", i + 1, exc_info.value.kind, cb.state)
    assert cb.state is CircuitState.OPEN


def test_open_fails_fast_without_calling_upstream():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.CONNECTION))

    called = []
    with pytest.raises(CircuitOpen) as exc_info:
        cb.call(lambda: called.append(1))
    logger.info("open circuit -> reason=%s retry_after=%s upstream_called=%s",
                exc_info.value.reason, exc_info.value.retry_after, bool(called))
    assert not called  # 快速失败，未触达上游
    assert exc_info.value.retry_after > 0


def test_half_open_probe_success_closes_circuit():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.BUSINESS))
    assert cb.state is CircuitState.OPEN

    clock.advance(10.0)  # 冷却期满
    assert cb.state is CircuitState.HALF_OPEN
    result = cb.call(lambda: "recovered")
    logger.info("half-open probe -> result=%s state=%s", result, cb.state)
    assert result == "recovered"
    assert cb.state is CircuitState.CLOSED


def test_half_open_probe_failure_reopens_and_retimes():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.TIMEOUT))
    clock.advance(10.0)
    with pytest.raises(UpstreamError):
        cb.call(fail_with(UpstreamFailureKind.TIMEOUT))
    logger.info("half-open probe failed -> state=%s", cb.state)
    assert cb.state is CircuitState.OPEN

    # 冷却重新计时：差一点也不放行
    clock.advance(9.9)
    with pytest.raises(CircuitOpen):
        cb.call(lambda: None)
    clock.advance(0.2)
    assert cb.state is CircuitState.HALF_OPEN


def test_failure_kinds_are_distinguishable():
    clock = FakeClock()
    cb = make_breaker(clock)
    kinds = [UpstreamFailureKind.TIMEOUT, UpstreamFailureKind.CONNECTION, UpstreamFailureKind.BUSINESS]
    for kind in kinds:
        with pytest.raises(UpstreamError) as exc_info:
            cb.call(fail_with(kind))
        logger.info("kind=%s -> reason=%s", kind, exc_info.value.reason)
        assert exc_info.value.kind is kind
        assert exc_info.value.reason == f"upstream_{kind.value}"
    assert len({f"upstream_{k.value}" for k in kinds}) == 3


def test_success_resets_consecutive_failure_count():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(2):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.BUSINESS))
    cb.call(lambda: "ok")  # 成功清零
    for _ in range(2):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.BUSINESS))
    logger.info("after reset, 2 more failures -> state=%s", cb.state)
    assert cb.state is CircuitState.CLOSED  # 未达阈值 3


def test_concurrent_calls_during_open_are_all_rejected_consistently():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        with pytest.raises(UpstreamError):
            cb.call(fail_with(UpstreamFailureKind.CONNECTION))

    results: list[str] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            cb.call(lambda: "ok")
            outcome = "passed"
        except CircuitOpen as exc:
            outcome = f"circuit_open:{exc.retry_after > 0}"
        with lock:
            results.append(outcome)
        logger.info("thread=%d -> %s", tid, outcome)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.startswith("circuit_open:") for r in results)
    assert len(results) == 16
