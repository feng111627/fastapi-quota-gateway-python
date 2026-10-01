"""熔断器状态机：CLOSED -> OPEN -> HALF_OPEN -> CLOSED/OPEN。

转换约定（时间与判定均在单个 asyncio.Lock 内完成，保证并发结论一致）：
- CLOSED：连续失败达到 failure_threshold -> OPEN，记录 opened_at；
- OPEN：now-opened_at >= open_duration 时，首个探测请求把状态切到
  HALF_OPEN 并放行；OPEN 期间其余请求一律快速失败（breaker.open），
  不会向上游堆积；
- HALF_OPEN：最多 half_open_max_probes 个探测请求在途，其余快速失败；
  成功累计达 success_threshold -> CLOSED（计数清零）；
  任意一次失败 -> 立刻回到 OPEN 并重置 opened_at（重新计时）。
时钟外部注入（now），便于确定性测试。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from app.config import BreakerConfig

logger = logging.getLogger("app.resilience")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerSnapshot:
    state: BreakerState
    consecutive_failures: int
    consecutive_successes: int
    opened_at: float | None
    probes_in_flight: int


@dataclass(frozen=True)
class BreakerVerdict:
    allowed: bool
    state: BreakerState
    reason: str | None = None


class CircuitBreaker:
    def __init__(self, name: str, config: BreakerConfig) -> None:
        self.name = name
        self.config = config
        self._lock = asyncio.Lock()
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.opened_at: float | None = None
        self.probes_in_flight = 0

    def _snapshot_locked(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            self.state, self.consecutive_failures, self.consecutive_successes,
            self.opened_at, self.probes_in_flight,
        )

    def current_state(self, now: float) -> BreakerState:
        """注意：纯读不加锁，仅用于观测；判定以 allow() 为准。"""
        if self.state is BreakerState.OPEN and self.opened_at is not None and \
                now - self.opened_at >= self.config.open_duration:
            return BreakerState.HALF_OPEN
        return self.state

    async def allow(self, now: float) -> BreakerVerdict:
        async with self._lock:
            if self.state is BreakerState.CLOSED:
                return BreakerVerdict(True, BreakerState.CLOSED)

            if self.state is BreakerState.OPEN:
                if self.opened_at is not None and \
                        now - self.opened_at >= self.config.open_duration:
                    self.state = BreakerState.HALF_OPEN
                    self.consecutive_successes = 0
                    self.probes_in_flight = 1
                    logger.info("熔断 %s OPEN->HALF_OPEN 放行探测请求 now=%.3f",
                                self.name, now)
                    return BreakerVerdict(True, BreakerState.HALF_OPEN)
                logger.info("熔断 %s OPEN 快速失败 now=%.3f", self.name, now)
                return BreakerVerdict(False, BreakerState.OPEN, "breaker.open")

            # HALF_OPEN
            if self.probes_in_flight < self.config.half_open_max_probes:
                self.probes_in_flight += 1
                logger.info("熔断 %s HALF_OPEN 放行探测 在途=%d", self.name,
                            self.probes_in_flight)
                return BreakerVerdict(True, BreakerState.HALF_OPEN)
            logger.info("熔断 %s HALF_OPEN 探测名额已满，快速失败", self.name)
            return BreakerVerdict(False, BreakerState.HALF_OPEN, "breaker.open")

    async def record_success(self, now: float) -> None:
        async with self._lock:
            if self.state is BreakerState.HALF_OPEN:
                self.probes_in_flight = max(0, self.probes_in_flight - 1)
                self.consecutive_successes += 1
                if self.consecutive_successes >= self.config.success_threshold:
                    self._reset_closed_locked()
                    logger.info("熔断 %s HALF_OPEN->CLOSED 探测成功，恢复", self.name)
            else:
                self.consecutive_failures = 0

    async def record_failure(self, now: float) -> None:
        async with self._lock:
            if self.state is BreakerState.HALF_OPEN:
                self.probes_in_flight = max(0, self.probes_in_flight - 1)
                self.state = BreakerState.OPEN
                self.opened_at = now
                self.consecutive_successes = 0
                logger.info("熔断 %s HALF_OPEN->OPEN 探测失败，重新计时 now=%.3f",
                            self.name, now)
                return
            self.consecutive_failures += 1
            if (self.state is BreakerState.CLOSED and
                    self.consecutive_failures >= self.config.failure_threshold):
                self.state = BreakerState.OPEN
                self.opened_at = now
                logger.info("熔断 %s CLOSED->OPEN 连续失败=%d 达到阈值=%d",
                            self.name, self.consecutive_failures,
                            self.config.failure_threshold)

    def _reset_closed_locked(self) -> None:
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.opened_at = None
        self.probes_in_flight = 0

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            self.state, self.consecutive_failures, self.consecutive_successes,
            self.opened_at, self.probes_in_flight,
        )
