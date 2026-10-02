"""接入层编排：限流 -> 版本协商 -> 额度扣减 -> 熔断保护的上游调用 -> 失败退还。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from .circuitbreaker import CircuitBreaker
from .errors import (
    CircuitOpen,
    RateLimitExceeded,
    RateLimitReason,
    UpstreamError,
    UpstreamFailureKind,
)
from .quota import ChargeResult, CostPolicy, CostResolution, TieredQuotaManager
from .ratelimit import MultiLevelRateLimiter
from .versioning import NegotiationResult, VersionRegistry

T = TypeVar("T")

# 这些失败类别下请求"白扣了"，额度退还给客户
_REFUNDABLE_KINDS = frozenset({UpstreamFailureKind.TIMEOUT, UpstreamFailureKind.CONNECTION})


@dataclass(frozen=True)
class QuotaInfo:
    """成功响应携带的额度视图。"""

    plan: str
    limit: int
    remaining: float
    reset_at: float
    cost: float


@dataclass(frozen=True)
class GatewayResponse:
    body: object
    version: NegotiationResult
    upstream_kind: str = "ok"
    quota: QuotaInfo | None = None
    cost: CostResolution | None = None


class Gateway:
    """面向客户端调用的统一入口。

    判定顺序固定为：限流 -> 版本协商 -> 额度扣减 -> 熔断 -> 上游调用，
    保证同一请求在任何并发时序下得到一致、可解释的结论。

    - 额度不足拒绝时归还已扣的速率令牌，反复拦截结论可复现。
    - 上游超时/连接失败、熔断降级时退还本次额度（按 request_id 幂等）。
    - quota/costs 为 None 时行为与旧版完全一致。
    """

    def __init__(
        self,
        limiter: MultiLevelRateLimiter,
        versions: VersionRegistry,
        breaker: CircuitBreaker,
        *,
        quota: TieredQuotaManager | None = None,
        costs: CostPolicy | None = None,
    ) -> None:
        self._limiter = limiter
        self._versions = versions
        self._breaker = breaker
        self._quota = quota
        self._costs = costs

    def handle(
        self,
        client_id: str,
        endpoint: str,
        requested_version: str | None,
        upstream: Callable[[], T],
        *,
        declared_cost: float | None = None,
        request_id: str | None = None,
    ) -> GatewayResponse:
        """处理一次客户端调用。失败时抛出 GatewayError 子类，reason 可区分。"""
        resolution = self._resolve_cost(endpoint, declared_cost)
        cost = resolution.cost if resolution else 1.0

        # 1. 速率限流（按权重扣令牌）
        decision = self._limiter.check(client_id, endpoint, cost=cost)
        if not decision.allowed:
            raise RateLimitExceeded(
                decision.reason,
                retry_after=decision.retry_after,
                detail=self._detail(client_id, endpoint, resolution),
            )

        # 2. 版本协商（拒绝不消耗额度）
        try:
            version = self._versions.negotiate(requested_version)
        except Exception as exc:
            self._attach_quota(exc, client_id)
            raise

        # 3. 总额度扣减（按 request_id 幂等）
        charge = None
        if self._quota is not None:
            charge = self._quota.charge(client_id, cost, request_id)
            if not charge.allowed:
                # 额度不足不占用速率预算：归还令牌，保证重复拦截结论一致
                self._limiter.refund(client_id, endpoint, cost)
                raise RateLimitExceeded(
                    RateLimitReason.QUOTA_EXHAUSTED,
                    retry_after=charge.retry_after,
                    detail=self._detail(client_id, endpoint, resolution, charge),
                )

        # 4. 熔断 + 上游调用；失败按类别退还额度
        try:
            body = self._breaker.call(upstream)
        except CircuitOpen as exc:
            refunded = self._refund(client_id, cost, request_id, charge)
            exc.detail["quota"] = self._quota_view(client_id, charge, refunded)
            raise
        except UpstreamError as exc:
            refunded = False
            if exc.kind in _REFUNDABLE_KINDS:
                refunded = self._refund(client_id, cost, request_id, charge)
            exc.detail["quota"] = self._quota_view(client_id, charge, refunded)
            raise

        return GatewayResponse(
            body=body,
            version=version,
            quota=self._quota_info(charge),
            cost=resolution,
        )

    # ---- 内部 ----

    def _resolve_cost(self, endpoint: str, declared: float | None) -> CostResolution | None:
        if self._costs is None:
            return None
        return self._costs.resolve(endpoint, declared)

    def _refund(
        self,
        client_id: str,
        cost: float,
        request_id: str | None,
        charge: ChargeResult | None,
    ) -> bool:
        if self._quota is None or charge is None or not charge.allowed:
            return False
        if request_id is not None:
            return self._quota.refund(client_id, request_id)
        self._quota.refund_charge(client_id, cost)
        return True

    def _quota_view(
        self, client_id: str, charge: ChargeResult | None, refunded: bool
    ) -> dict | None:
        if self._quota is None:
            return None
        status = self._quota.status(client_id)
        return {
            "plan": status.plan,
            "limit": status.limit,
            "remaining": status.remaining,
            "reset_at": status.reset_at,
            "refunded": refunded,
        }

    def _quota_info(self, charge: ChargeResult | None) -> QuotaInfo | None:
        if charge is None:
            return None
        return QuotaInfo(
            plan=charge.status.plan,
            limit=charge.status.limit,
            remaining=charge.status.remaining,
            reset_at=charge.status.reset_at,
            cost=charge.cost,
        )

    def _detail(
        self,
        client_id: str,
        endpoint: str,
        resolution: CostResolution | None,
        charge: ChargeResult | None = None,
    ) -> dict:
        detail: dict = {"client_id": client_id, "endpoint": endpoint}
        if resolution is not None:
            detail["cost"] = {
                "applied": resolution.cost,
                "weight": resolution.weight,
                "declared": resolution.declared,
                "clamped": resolution.clamped,
            }
        if charge is not None:
            detail["quota"] = {
                "plan": charge.status.plan,
                "limit": charge.status.limit,
                "remaining": charge.status.remaining,
                "reset_at": charge.status.reset_at,
            }
        elif self._quota is not None:
            status = self._quota.status(client_id)
            detail["quota"] = {
                "plan": status.plan,
                "limit": status.limit,
                "remaining": status.remaining,
                "reset_at": status.reset_at,
            }
        return detail

    def _attach_quota(self, exc: Exception, client_id: str) -> None:
        """给已构造好的异常（如版本拒绝）补充额度视图，统一对外口径。"""
        if self._quota is None:
            return
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict):
            status = self._quota.status(client_id)
            detail["quota"] = {
                "plan": status.plan,
                "limit": status.limit,
                "remaining": status.remaining,
                "reset_at": status.reset_at,
            }
