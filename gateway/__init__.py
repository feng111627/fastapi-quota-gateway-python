"""客户端接入层：分档计费、限流、版本协商、熔断降级。"""

from .billing import CostResolver, EndpointCostPolicy, ResolvedCost
from .circuitbreaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from .errors import (
    BillingRejected,
    BillingRejectReason,
    CircuitOpen,
    GatewayError,
    QuotaExhausted,
    QuotaReason,
    RateLimitExceeded,
    RateLimitReason,
    UpstreamError,
    UpstreamFailureKind,
    VersionRejected,
    VersionRejectReason,
)
from .gate import AccountingGate, QuotaView
from .plans import Plan, PlanRegistry, QuotaResetMode
from .quota import QuotaLedger, QuotaStatusView
from .ratelimit import LimitDecision, MultiLevelRateLimiter, RateLimitPolicy, TokenBucket
from .service import Gateway, GatewayResponse, QuotaGateway, QuotaGatewayResponse
from .versioning import ApiVersion, NegotiationResult, VersionRegistry, VersionRule, VersionStatus

__all__ = [
    "AccountingGate",
    "ApiVersion",
    "BillingRejected",
    "BillingRejectReason",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpen",
    "CircuitState",
    "CostResolver",
    "EndpointCostPolicy",
    "Gateway",
    "GatewayError",
    "GatewayResponse",
    "LimitDecision",
    "MultiLevelRateLimiter",
    "NegotiationResult",
    "Plan",
    "PlanRegistry",
    "QuotaExhausted",
    "QuotaGateway",
    "QuotaGatewayResponse",
    "QuotaLedger",
    "QuotaReason",
    "QuotaResetMode",
    "QuotaStatusView",
    "QuotaView",
    "RateLimitExceeded",
    "RateLimitPolicy",
    "RateLimitReason",
    "ResolvedCost",
    "TokenBucket",
    "UpstreamError",
    "UpstreamFailureKind",
    "VersionRegistry",
    "VersionRejected",
    "VersionRejectReason",
    "VersionRule",
    "VersionStatus",
]
