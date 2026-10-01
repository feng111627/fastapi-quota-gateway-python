"""熔断器状态转换测试：闭合/打开/半开/恢复 与并发一致性。"""

import asyncio
import logging

from app.config import BreakerConfig
from app.resilience.breaker import BreakerState, CircuitBreaker

logger = logging.getLogger("tests.breaker")


def make_breaker(threshold=3, open_duration=5.0, probes=1, success_threshold=1):
    return CircuitBreaker("echo", BreakerConfig(
        failure_threshold=threshold,
        open_duration=open_duration,
        half_open_max_probes=probes,
        success_threshold=success_threshold,
    ))


def test_closed_to_open_after_threshold() -> None:
    async def scenario():
        cb = make_breaker(threshold=3)
        for i in range(2):
            await cb.record_failure(0.0)
        still_closed = (await cb.allow(0.0)).allowed
        await cb.record_failure(0.0)
        rejected = await cb.allow(0.0)
        return still_closed, rejected

    still_closed, rejected = asyncio.run(scenario())
    logger.info("输入 连续3次失败：第3次前放行=%s，熔断打开后放行=%s 原因=%s",
                still_closed, rejected.allowed, rejected.reason)
    assert still_closed is True
    assert rejected.allowed is False
    assert rejected.state is BreakerState.OPEN
    assert rejected.reason == "breaker.open"


def test_open_fast_fails_until_cooldown() -> None:
    async def scenario():
        cb = make_breaker(open_duration=5.0)
        for _ in range(3):
            await cb.record_failure(0.0)
        at4 = await cb.allow(4.999)
        probe = await cb.allow(5.0)
        return at4, probe

    at4, probe = asyncio.run(scenario())
    logger.info("输入 冷却期内(4.999s)放行=%s；到5.0s探测放行=%s 状态=%s",
                at4.allowed, probe.allowed, probe.state.value)
    assert at4.allowed is False
    assert probe.allowed is True
    assert probe.state is BreakerState.HALF_OPEN


def test_half_open_single_probe_others_rejected() -> None:
    async def scenario():
        cb = make_breaker(probes=1, open_duration=5.0)
        for _ in range(3):
            await cb.record_failure(0.0)
        first = await cb.allow(5.0)
        second = await cb.allow(5.0)
        return first, second

    first, second = asyncio.run(scenario())
    logger.info("输入 HALF_OPEN 并发两请求：探测名额1，放行=%s/%s",
                first.allowed, second.allowed)
    assert first.allowed is True
    assert second.allowed is False


def test_half_open_success_recovers() -> None:
    async def scenario():
        cb = make_breaker(open_duration=5.0)
        for _ in range(3):
            await cb.record_failure(0.0)
        await cb.allow(5.0)
        await cb.record_success(5.1)
        return await cb.allow(5.2), cb.snapshot()

    verdict, snapshot = asyncio.run(scenario())
    logger.info("输入 探测成功：状态=%s 失败计数=%d", snapshot.state.value,
                snapshot.consecutive_failures)
    assert verdict.allowed is True
    assert snapshot.state is BreakerState.CLOSED
    assert snapshot.consecutive_failures == 0


def test_half_open_failure_reopens_and_re_times() -> None:
    async def scenario():
        cb = make_breaker(open_duration=5.0)
        for _ in range(3):
            await cb.record_failure(0.0)
        await cb.allow(5.0)
        await cb.record_failure(5.5)
        immediate = await cb.allow(5.6)
        reprobe = await cb.allow(10.5)
        return immediate, reprobe, cb.opened_at

    immediate, reprobe, opened_at = asyncio.run(scenario())
    logger.info("输入 探测失败：重新打开 opened_at=%s，5.6放行=%s，10.5再探测=%s",
                opened_at, immediate.allowed, reprobe.allowed)
    assert opened_at == 5.5
    assert immediate.allowed is False
    assert reprobe.allowed is True


def test_concurrent_open_requests_only_one_probe() -> None:
    """恢复边界并发：只有一个请求能拿到探测名额。"""
    async def scenario():
        cb = make_breaker(probes=1, open_duration=5.0)
        for _ in range(3):
            await cb.record_failure(0.0)
        return await asyncio.gather(*[cb.allow(5.0) for _ in range(50)])

    verdicts = asyncio.run(scenario())
    probes = sum(1 for v in verdicts if v.allowed)
    logger.info("输入 恢复瞬间50并发，获得探测名额数=%d", probes)
    assert probes == 1
