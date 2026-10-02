"""客户端接入层：分档配额、权重计费、限流、版本协商、熔断降级。"""

from .circuitbreaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from .errors import (
    CircuitOpen,
    GatewayError,
    RateLimitExceeded,
    RateLimitReason,
    UpstreamError,
    UpstreamFailureKind,
    VersionRejected,
    VersionRejectReason,
)
from .quota import (
    ChargeResult,
    CostPolicy,
    CostResolution,
    QuotaPeriod,
    QuotaPlan,
    QuotaStatus,
    TieredQuotaManager,
)
from .ratelimit import LimitDecision, MultiLevelRateLimiter, RateLimitPolicy, TokenBucket
from .service import Gateway, GatewayResponse, QuotaInfo
from .versioning import ApiVersion, NegotiationResult, VersionRegistry, VersionRule, VersionStatus

__all__ = [
    "ApiVersion",
    "ChargeResult",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpen",
    "CircuitState",
    "CostPolicy",
    "CostResolution",
    "Gateway",
    "GatewayError",
    "GatewayResponse",
    "LimitDecision",
    "MultiLevelRateLimiter",
    "NegotiationResult",
    "QuotaInfo",
    "QuotaPeriod",
    "QuotaPlan",
    "QuotaStatus",
    "RateLimitExceeded",
    "RateLimitPolicy",
    "RateLimitReason",
    "TieredQuotaManager",
    "TokenBucket",
    "UpstreamError",
    "UpstreamFailureKind",
    "VersionRegistry",
    "VersionRejected",
    "VersionRejectReason",
    "VersionRule",
    "VersionStatus",
]
