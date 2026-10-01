"""令牌桶单元测试：突发上限、稳态补充、retry_after、重启恢复。"""

import logging

from app.config import QuotaRule
from app.quota.bucket import TokenBucket

logger = logging.getLogger("tests.bucket")


def test_burst_capacity_is_not_exceeded() -> None:
    bucket = TokenBucket(QuotaRule(rate=0.0, burst=3.0))
    decisions = [bucket.try_acquire(0.0) for _ in range(4)]
    logger.info("输入 rate=0 burst=3 连续4次申请，判定=%s",
                [d.allowed for d in decisions])
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[3].retry_after == float("inf")


def test_refill_at_steady_rate() -> None:
    bucket = TokenBucket(QuotaRule(rate=2.0, burst=3.0))
    for _ in range(3):
        assert bucket.try_acquire(0.0).allowed
    assert not bucket.try_acquire(0.0).allowed
    after_04 = bucket.try_acquire(0.4)
    logger.info("输入 0.4s 后申请（补充0.8枚），retry_after=%.3f allowed=%s",
                after_04.retry_after, after_04.allowed)
    assert not after_04.allowed
    assert abs(after_04.retry_after - 0.1) < 1e-6
    assert bucket.try_acquire(0.5).allowed  # 0.5s 补 1 枚，可放行


def test_refill_never_exceeds_burst() -> None:
    bucket = TokenBucket(QuotaRule(rate=10.0, burst=2.0))
    bucket.try_acquire(100.0)
    bucket.try_acquire(200.0)
    assert bucket.tokens <= 2.0 + 1e-9


def test_restore_refills_during_downtime() -> None:
    rule = QuotaRule(rate=1.0, burst=5.0)
    restored = TokenBucket.restore(
        rule, {"tokens": 0.0, "last_refill": 1000.0}, now=1003.0
    )
    logger.info("输入 停机3s rate=1 恢复后令牌=%.2f", restored.tokens)
    assert abs(restored.tokens - 3.0) < 1e-9


def test_clock_rewind_does_not_lose_tokens() -> None:
    bucket = TokenBucket(QuotaRule(rate=1.0, burst=5.0), now=100.0, tokens=2.0)
    bucket.try_acquire(50.0)  # 时钟回退
    assert bucket.tokens >= 1.0
