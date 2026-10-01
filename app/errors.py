"""统一错误码与异常类型（类型定义 + 最小辅助函数）。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """可区分的拒绝/失败原因。"""

    # 限流类
    RATE_LIMITED_CLIENT = "rate_limited.client"
    RATE_LIMITED_API = "rate_limited.api"
    RATE_LIMITED_CLIENT_API = "rate_limited.client_api"

    # 版本类
    VERSION_MISSING = "version.missing"
    VERSION_INVALID = "version.invalid"
    VERSION_UNSUPPORTED = "version.unsupported"

    # 上游类
    UPSTREAM_TIMEOUT = "upstream.timeout"
    UPSTREAM_CONNECTION = "upstream.connection"
    UPSTREAM_BUSINESS = "upstream.business"
    BREAKER_OPEN = "breaker.open"

    # 通用
    INTERNAL_ERROR = "internal.error"


class GatewayError(Exception):
    """网关层统一异常基类。"""

    def __init__(self, code: ErrorCode, message: str, *, status_code: int = 400,
                 extra: dict | None = None, headers: dict[str, str] | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}
        self.headers = headers or {}
        super().__init__(message)
