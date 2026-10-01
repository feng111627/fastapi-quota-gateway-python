"""接入层编排：限流 -> 版本协商 -> 熔断保护的上游调用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from .circuitbreaker import CircuitBreaker
from .errors import RateLimitExceeded
from .ratelimit import MultiLevelRateLimiter
from .versioning import NegotiationResult, VersionRegistry

T = TypeVar("T")


@dataclass(frozen=True)
class GatewayResponse:
    body: object
    version: NegotiationResult
    upstream_kind: str = "ok"


class Gateway:
    """面向客户端调用的统一入口。

    判定顺序固定为：限流 -> 版本协商 -> 熔断 -> 上游调用，
    保证同一请求在任何并发时序下得到一致、可解释的结论。
    """

    def __init__(
        self,
        limiter: MultiLevelRateLimiter,
        versions: VersionRegistry,
        breaker: CircuitBreaker,
    ) -> None:
        self._limiter = limiter
        self._versions = versions
        self._breaker = breaker

    def handle(
        self,
        client_id: str,
        endpoint: str,
        requested_version: str | None,
        upstream: Callable[[], T],
    ) -> GatewayResponse:
        """处理一次客户端调用。失败时抛出 GatewayError 子类，reason 可区分。"""
        decision = self._limiter.check(client_id, endpoint)
        if not decision.allowed:
            raise RateLimitExceeded(
                decision.reason, retry_after=decision.retry_after,
                detail={"client_id": client_id, "endpoint": endpoint},
            )

        version = self._versions.negotiate(requested_version)

        body = self._breaker.call(upstream)
        return GatewayResponse(body=body, version=version)
