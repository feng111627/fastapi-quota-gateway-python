"""熔断与降级：连续失败快速失败，冷却后半开探测恢复。"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .errors import CircuitOpen, UpstreamError, UpstreamFailureKind

T = TypeVar("T")


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 3  # 连续失败达到该次数后熔断
    recovery_timeout: float = 30.0  # 熔断后多少秒进入半开
    half_open_max_calls: int = 1  # 半开时允许的探测并发数


class CircuitBreaker:
    """线程安全熔断器。

    - CLOSED：正常放行；连续失败达阈值 -> OPEN。
    - OPEN：快速失败抛 CircuitOpen；冷却期满 -> HALF_OPEN。
    - HALF_OPEN：放行有限探测请求；探测成功 -> CLOSED，失败 -> OPEN（重新计时）。
    只统计上游失败（UpstreamError：超时/连接/业务错误），本地拒绝与非上游异常不计入。
    """

    def __init__(self, config: CircuitBreakerConfig, *, clock: Callable[[], float] | None = None) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._effective_state(self._clock())

    def call(self, fn: Callable[[], T]) -> T:
        """通过熔断器调用上游；失败时按 UpstreamError.kind 归类并计数。"""
        probe = self._before_call()
        try:
            result = fn()
        except UpstreamError as exc:
            self._on_failure(exc.kind, probe)
            raise
        except BaseException:
            # 非上游异常不计入熔断统计，但要释放半开探测名额
            if probe:
                self._release_probe()
            raise
        self._on_success(probe)
        return result

    # ---- 内部状态机（均在锁内执行） ----

    def _effective_state(self, now: float) -> CircuitState:
        if self._state is CircuitState.OPEN and now - self._opened_at >= self._config.recovery_timeout:
            return CircuitState.HALF_OPEN
        return self._state

    def _before_call(self) -> bool:
        """返回本次调用是否为半开探测。"""
        now = self._clock()
        with self._lock:
            effective = self._effective_state(now)
            if effective is CircuitState.OPEN:
                retry_after = self._config.recovery_timeout - (now - self._opened_at)
                raise CircuitOpen(retry_after=round(max(retry_after, 0.0), 3))
            if effective is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self._config.half_open_max_calls:
                    raise CircuitOpen(retry_after=round(self._config.recovery_timeout, 3))
                # 正式进入半开（首次从 OPEN 迁移时重置打开时间基准）
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight += 1
                return True
            return False

    def _on_success(self, probe: bool) -> None:
        with self._lock:
            if probe:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED

    def _on_failure(self, kind: UpstreamFailureKind, probe: bool) -> None:
        now = self._clock()
        with self._lock:
            if probe:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                # 半开探测失败：立即回到 OPEN 并重新计时冷却
                self._state = CircuitState.OPEN
                self._opened_at = now
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now

    def _release_probe(self) -> None:
        with self._lock:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
