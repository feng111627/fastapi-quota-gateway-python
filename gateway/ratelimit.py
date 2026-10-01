"""多级令牌桶限流：客户端与接口两个维度，支持突发与状态持久化。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .errors import RateLimitReason


@dataclass(frozen=True)
class RateLimitPolicy:
    """单级限流策略：稳态速率 + 突发容量。"""

    rate_per_sec: float
    burst: int


@dataclass
class TokenBucket:
    """令牌桶（线程安全由外层 Limiter 的锁保证）。"""

    capacity: float
    rate_per_sec: float
    tokens: float = 0.0
    last_refill: float = 0.0

    def _refill(self, now: float) -> None:
        if self.last_refill == 0.0:
            # 新桶按满容量起步，允许初始突发
            self.tokens = self.capacity
            self.last_refill = now
            return
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
        self.last_refill = now

    def try_consume(self, now: float, cost: float = 1.0) -> tuple[bool, float]:
        """尝试扣减 cost 个令牌。返回 (是否成功, 失败时的建议等待秒数)。"""
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        deficit = cost - self.tokens
        wait = deficit / self.rate_per_sec if self.rate_per_sec > 0 else float("inf")
        return False, wait

    def to_snapshot(self) -> dict:
        return {
            "capacity": self.capacity,
            "rate_per_sec": self.rate_per_sec,
            "tokens": self.tokens,
            "last_refill": self.last_refill,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "TokenBucket":
        return cls(
            capacity=float(data["capacity"]),
            rate_per_sec=float(data["rate_per_sec"]),
            tokens=float(data["tokens"]),
            last_refill=float(data["last_refill"]),
        )


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: RateLimitReason | None = None
    retry_after: float = 0.0


class MultiLevelRateLimiter:
    """按 (client, endpoint) 两个维度做多级限流。

    - 每个维度独立的令牌桶；任一维度超限即拒绝。
    - 检查与扣减在同一把锁内原子完成：先全部试扣，任一失败则回滚，
      保证并发下不超发、不重复扣减。
    - 状态可快照到磁盘，进程重启后按快照恢复（令牌数与上次补充时间）。
    """

    def __init__(
        self,
        client_policy: RateLimitPolicy,
        endpoint_policies: dict[str, RateLimitPolicy],
        *,
        state_path: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client_policy = client_policy
        self._endpoint_policies = dict(endpoint_policies)
        self._state_path = state_path
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._client_buckets: dict[str, TokenBucket] = {}
        self._endpoint_buckets: dict[str, TokenBucket] = {}
        if state_path:
            self._load(state_path)

    def _new_client_bucket(self) -> TokenBucket:
        return TokenBucket(
            capacity=float(self._client_policy.burst),
            rate_per_sec=self._client_policy.rate_per_sec,
        )

    def _new_endpoint_bucket(self, endpoint: str) -> TokenBucket:
        policy = self._endpoint_policies[endpoint]
        return TokenBucket(capacity=float(policy.burst), rate_per_sec=policy.rate_per_sec)

    def check(self, client_id: str, endpoint: str) -> LimitDecision:
        """原子地完成"检查 + 扣减"；失败时回滚已扣令牌。"""
        now = self._clock()
        with self._lock:
            client_bucket = self._client_buckets.get(client_id)
            if client_bucket is None:
                client_bucket = self._client_buckets[client_id] = self._new_client_bucket()

            endpoint_bucket = None
            if endpoint in self._endpoint_policies:
                endpoint_bucket = self._endpoint_buckets.get(endpoint)
                if endpoint_bucket is None:
                    endpoint_bucket = self._endpoint_buckets[endpoint] = self._new_endpoint_bucket(endpoint)

            # 先在不提交的情况下检查各级（try_consume 会直接改状态，故记录快照以便回滚）
            client_before = (client_bucket.tokens, client_bucket.last_refill)
            ok_client, wait_client = client_bucket.try_consume(now)

            endpoint_before = None
            ok_endpoint, wait_endpoint = True, 0.0
            if endpoint_bucket is not None:
                endpoint_before = (endpoint_bucket.tokens, endpoint_bucket.last_refill)
                ok_endpoint, wait_endpoint = endpoint_bucket.try_consume(now)

            if ok_client and ok_endpoint:
                return LimitDecision(allowed=True)

            # 任一失败：回滚两个桶到检查前状态，避免"检查即扣减"造成重复扣减
            client_bucket.tokens, client_bucket.last_refill = client_before
            if endpoint_bucket is not None and endpoint_before is not None:
                endpoint_bucket.tokens, endpoint_bucket.last_refill = endpoint_before

            # 接口级优先报告：更具体的维度先暴露给调用方
            if not ok_endpoint:
                return LimitDecision(
                    allowed=False,
                    reason=RateLimitReason.ENDPOINT_RATE_EXCEEDED,
                    retry_after=round(wait_endpoint, 3),
                )
            return LimitDecision(
                allowed=False,
                reason=RateLimitReason.CLIENT_RATE_EXCEEDED,
                retry_after=round(wait_client, 3),
            )

    def snapshot(self) -> None:
        """把当前桶状态持久化到 state_path（若配置）。原子写入避免半截文件。"""
        if not self._state_path:
            return
        with self._lock:
            data = {
                "clients": {k: b.to_snapshot() for k, b in self._client_buckets.items()},
                "endpoints": {k: b.to_snapshot() for k, b in self._endpoint_buckets.items()},
            }
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._state_path)

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._client_buckets = {k: TokenBucket.from_snapshot(v) for k, v in data.get("clients", {}).items()}
        self._endpoint_buckets = {k: TokenBucket.from_snapshot(v) for k, v in data.get("endpoints", {}).items()}
