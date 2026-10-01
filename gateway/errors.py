"""错误分类体系：限流、版本协商、上游调用三类错误的可区分原因。"""

from __future__ import annotations

import enum


class RateLimitReason(enum.Enum):
    """限流拒绝原因。"""

    CLIENT_RATE_EXCEEDED = "client_rate_exceeded"
    ENDPOINT_RATE_EXCEEDED = "endpoint_rate_exceeded"


class VersionRejectReason(enum.Enum):
    """版本协商拒绝原因。"""

    VERSION_TOO_OLD = "version_too_old"
    VERSION_TOO_NEW = "version_too_new"
    VERSION_MALFORMED = "version_malformed"


class UpstreamFailureKind(enum.Enum):
    """上游调用失败的可区分原因。"""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BUSINESS = "business"


class GatewayError(Exception):
    """接入层错误基类，携带机器可读的 reason 与 HTTP 状态码。"""

    status_code: int = 500
    reason: str = "internal_error"

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        super().__init__(message or self.reason)
        self.message = message or self.reason
        self.detail = detail or {}


class RateLimitExceeded(GatewayError):
    status_code = 429

    def __init__(self, reason: RateLimitReason, *, retry_after: float, detail: dict | None = None) -> None:
        self.limit_reason = reason
        self.retry_after = retry_after
        super().__init__(
            f"rate limit exceeded: {reason.value}",
            detail={"retry_after": retry_after, **(detail or {})},
        )
        self.reason = reason.value


class VersionRejected(GatewayError):
    status_code = 400

    def __init__(self, reason: VersionRejectReason, *, requested: str, supported: list[str]) -> None:
        self.version_reason = reason
        super().__init__(
            f"version rejected: {reason.value}",
            detail={"requested": requested, "supported": supported},
        )
        self.reason = reason.value


class UpstreamError(GatewayError):
    status_code = 502

    def __init__(self, kind: UpstreamFailureKind, message: str = "") -> None:
        self.kind = kind
        super().__init__(message or f"upstream failure: {kind.value}")
        self.reason = f"upstream_{kind.value}"


class CircuitOpen(GatewayError):
    status_code = 503
    reason = "circuit_open"

    def __init__(self, *, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__("circuit breaker is open", detail={"retry_after": retry_after})
