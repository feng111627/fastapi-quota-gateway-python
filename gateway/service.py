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


# ---------------------------------------------------------------------------
# 分档计费网关
# ---------------------------------------------------------------------------

from .billing import CostResolver
from .errors import (  # noqa: E402
    CircuitOpen,
    UpstreamError,
    UpstreamFailureKind,
)
from .gate import AccountingGate, ReplayDirective  # noqa: E402


@dataclass(frozen=True)
class QuotaGatewayResponse:
    """分档计费网关的统一返回：结论 + 代价 + 额度视图，字段在任何结局下都齐全。"""

    body: object
    version: NegotiationResult
    cost: float
    replayed: bool
    quota: object  # QuotaView

    def to_envelope(self) -> dict:
        """成功响应对外信封（与错误信封字段对齐）。"""
        view = self.quota.to_dict()
        return {
            "status": "ok",
            "body": self.body,
            "version": str(self.version.version),
            "cost": round(self.cost, 3),
            "replayed": self.replayed,
            "quota": view,
        }


class QuotaGateway:
    """分档套餐 + 精细计费的统一入口。

    判定顺序：解析代价 -> 幂等登记/重放 -> 速率+总量预占 -> 版本协商
              -> 熔断 -> 上游调用。
    各失败结局的账目处理固定且可复现：
    - 代价非法/版本拒绝：请求未受理，预占全额回滚，幂等登记清除；
    - 熔断 OPEN：不触达上游，按 circuit_open 退还（重试时直接重放降级结论）；
    - 上游超时/连接失败：退还，原错误上抛，结论被幂等记录；
    - 上游业务失败：不退还（请求确实消耗了资源）；
    - 成功：计费落定，结论可重放。
    """

    def __init__(
        self,
        gate: AccountingGate,
        versions: VersionRegistry,
        breaker: CircuitBreaker,
        costs: CostResolver | None = None,
    ) -> None:
        self._gate = gate
        self._versions = versions
        self._breaker = breaker
        self._costs = costs or CostResolver()

    @property
    def gate(self) -> AccountingGate:
        return self._gate

    def quota_view(self, client_id: str):
        return self._gate.view(client_id)

    def handle(
        self,
        client_id: str,
        endpoint: str,
        requested_version: str | None,
        upstream: Callable[[], T],
        *,
        declared_cost: object = None,
        request_id: str | None = None,
    ) -> QuotaGatewayResponse:
        # 1) 代价解析（非法声明在此抛出，不进入任何计费流程）
        resolved = self._costs.resolve(endpoint, declared_cost)
        cost = resolved.cost

        # 2) 幂等登记 + 速率/总量预占；已有终态则重放
        replay = self._gate.begin_request(client_id, request_id, cost)
        if replay is not None:
            return self._handle_replay(client_id, endpoint, requested_version, replay, cost)

        # 3) 版本协商；拒绝时回滚预占（请求从未受理）
        try:
            version = self._versions.negotiate(requested_version)
        except BaseException:
            self._gate.abort(client_id, request_id, cost)
            raise

        # 4) 通过熔断器调用上游；按结局做固定、可复现的账目处理
        try:
            body = self._breaker.call(upstream)
        except CircuitOpen:
            # 降级：根本没触达上游，等同"请求未受理"——
            # 全额退还并清除幂等登记。恢复时刻由熔断器实时给出（异常自带），
            # 客户端稍后拿同一编号重试会在熔断器恢复后真正执行（不固化过期结论）。
            self._gate.abort(client_id, request_id, cost)
            raise
        except UpstreamError as exc:
            if exc.kind in (UpstreamFailureKind.TIMEOUT, UpstreamFailureKind.CONNECTION):
                # 超时/连接：没拿到数据，白扣的额度退还；失败结论固化供重放
                self._gate.fail_refund(
                    client_id, request_id, cost,
                    error_kind=exc.kind.value, error_message=exc.message,
                )
            else:
                # 业务失败：上游实际处理了请求，计费成立（不退），固化失败终态
                if request_id is not None:
                    self._gate.mark_terminal(
                        request_id,
                        success=False,
                        error_kind=exc.kind.value,
                        error_message=exc.message,
                    )
            raise
        except BaseException:
            # 非上游异常：不计入熔断，也不应由客户买单，按未受理回滚
            self._gate.abort(client_id, request_id, cost)
            raise

        # 5) 成功落定
        self._gate.commit(client_id, request_id, cost, body)
        return QuotaGatewayResponse(
            body=body,
            version=version,
            cost=cost,
            replayed=False,
            quota=self._gate.view(client_id),
        )

    def _handle_replay(
        self,
        client_id: str,
        endpoint: str,
        requested_version: str | None,
        replay: ReplayDirective,
        cost: float,
    ) -> QuotaGatewayResponse:
        """重放同一 request_id 的终态结论：不扣费、不触达上游、不扰动熔断。"""
        version = self._versions.negotiate(requested_version)
        if replay.success:
            return QuotaGatewayResponse(
                body=replay.body,
                version=version,
                cost=cost,
                replayed=True,
                quota=self._gate.view(client_id),
            )
        kind = replay.error_kind or "business"
        # 正常流程中降级结论不会被固化（handle 里 CircuitOpen 按未受理回滚），
        # 此处只为兼容旧快照做防御性处理。
        if kind == "circuit_open":
            raise CircuitOpen(retry_after=0.0)
        if kind in (UpstreamFailureKind.TIMEOUT.value, UpstreamFailureKind.CONNECTION.value):
            raise UpstreamError(UpstreamFailureKind(kind), replay.error_message)
        # 业务类失败：重放为业务错误
        try:
            failure_kind = UpstreamFailureKind(kind)
        except ValueError:
            failure_kind = UpstreamFailureKind.BUSINESS
        raise UpstreamError(failure_kind, replay.error_message)

