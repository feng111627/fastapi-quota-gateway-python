"""并发限流与配额恢复测试。"""

from __future__ import annotations

import logging
import threading

from gateway import MultiLevelRateLimiter, RateLimitPolicy, RateLimitReason

logger = logging.getLogger("test.ratelimit")


def make_limiter(**kwargs) -> MultiLevelRateLimiter:
    return MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=1.0, burst=5),
        endpoint_policies={"/api/data": RateLimitPolicy(rate_per_sec=2.0, burst=10)},
        **kwargs,
    )


def test_burst_allowed_then_rejected_with_distinct_reason():
    limiter = make_limiter()
    results = [limiter.check("c1", "/api/data") for _ in range(6)]
    for i, d in enumerate(results):
        logger.info("request #%d client=c1 endpoint=/api/data -> allowed=%s reason=%s retry_after=%s",
                    i, d.allowed, d.reason, d.retry_after)
    assert all(d.allowed for d in results[:5])  # burst=5 全部放行
    assert not results[5].allowed
    assert results[5].reason is RateLimitReason.CLIENT_RATE_EXCEEDED
    assert results[5].retry_after > 0


def test_endpoint_level_reason_distinguishable():
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=100.0, burst=100),
        endpoint_policies={"/api/data": RateLimitPolicy(rate_per_sec=1.0, burst=2)},
    )
    results = [limiter.check("c1", "/api/data") for _ in range(3)]
    for i, d in enumerate(results):
        logger.info("request #%d -> allowed=%s reason=%s", i, d.allowed, d.reason)
    assert results[2].reason is RateLimitReason.ENDPOINT_RATE_EXCEEDED


def test_concurrent_no_over_issue_and_no_double_deduction():
    """并发下通过数恰好等于配额，拒绝不扣减令牌。"""
    burst = 20
    limiter = MultiLevelRateLimiter(
        client_policy=RateLimitPolicy(rate_per_sec=0.0001, burst=burst),
        endpoint_policies={},
    )
    n_threads, per_thread = 8, 10
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        for i in range(per_thread):
            d = limiter.check(f"c-{tid % 2}", "/unlimited")  # 两个客户端各 burst=20
            with lock:
                outcomes.append(d.allowed)
            logger.info("thread=%d req=%d client=c-%d -> allowed=%s reason=%s",
                        tid, i, tid % 2, d.allowed, d.reason)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(outcomes)
    logger.info("total=%d allowed=%d expected=%d", len(outcomes), allowed, 2 * burst)
    assert allowed == 2 * burst  # 两个客户端各 20，不超发不少发


def test_state_restored_after_restart(tmp_path):
    state = tmp_path / "state.json"
    limiter = make_limiter(state_path=str(state))
    for _ in range(4):
        assert limiter.check("c1", "/api/data").allowed
    limiter.snapshot()

    # 模拟进程重启：新实例从快照恢复，c1 应只剩 1 个令牌
    restored = make_limiter(state_path=str(state))
    d1 = restored.check("c1", "/api/data")
    d2 = restored.check("c1", "/api/data")
    logger.info("after restart: d1=%s d2=%s reason=%s", d1.allowed, d2.allowed, d2.reason)
    assert d1.allowed
    assert not d2.allowed
    assert d2.reason is RateLimitReason.CLIENT_RATE_EXCEEDED
