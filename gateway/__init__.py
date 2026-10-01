"""客户端接入层：限流、版本协商、熔断降级。"""

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
from .ratelimit import LimitDecision, MultiLevelRateLimiter, RateLimitPolicy, TokenBucket
from .service import Gateway, GatewayResponse
from .versioning import ApiVersion, NegotiationResult, VersionRegistry, VersionRule, VersionStatus

__all__ = [
    "ApiVersion",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpen",
    "CircuitState",
    "Gateway",
    "GatewayError",
    "GatewayResponse",
    "LimitDecision",
    "MultiLevelRateLimiter",
    "NegotiationResult",
    "RateLimitExceeded",
    "RateLimitPolicy",
    "RateLimitReason",
    "TokenBucket",
    "UpstreamError",
    "UpstreamFailureKind",
    "VersionRegistry",
    "VersionRejected",
    "VersionRejectReason",
    "VersionRule",
    "VersionStatus",
]
