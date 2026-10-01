"""上游异常分类：超时 / 连接失败 / 业务错误，原因可区分。

分类约定：
- httpx.TimeoutException（含 ConnectTimeout/ReadTimeout） -> TIMEOUT
- httpx.TransportError 中的连接类（ConnectError、ProxyError、
  PoolTimeout/ReadError 等传输层问题）                       -> CONNECTION
- 上游可达但返回非 2xx：UpstreamStatusError                 -> BUSINESS
- 其它未预期错误                                             -> UNKNOWN
注意：ConnectTimeout 优先归为 TIMEOUT（它是 TimeoutException 子类）。
"""

from __future__ import annotations

import httpx

from app.errors import ErrorCode

from enum import Enum


class UpstreamErrorKind(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BUSINESS = "business"
    UNKNOWN = "unknown"


class UpstreamStatusError(Exception):
    """上游可达但返回非 2xx 业务/HTTP 错误。"""

    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"上游返回状态码 {status_code}")


_KIND_TO_CODE: dict[UpstreamErrorKind, ErrorCode] = {
    UpstreamErrorKind.TIMEOUT: ErrorCode.UPSTREAM_TIMEOUT,
    UpstreamErrorKind.CONNECTION: ErrorCode.UPSTREAM_CONNECTION,
    UpstreamErrorKind.BUSINESS: ErrorCode.UPSTREAM_BUSINESS,
}


def kind_to_code(kind: UpstreamErrorKind) -> ErrorCode:
    return _KIND_TO_CODE.get(kind, ErrorCode.INTERNAL_ERROR)


def classify(exc: Exception) -> UpstreamErrorKind:
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamErrorKind.TIMEOUT
    if isinstance(exc, UpstreamStatusError):
        return UpstreamErrorKind.BUSINESS
    if isinstance(exc, httpx.TransportError):
        return UpstreamErrorKind.CONNECTION
    return UpstreamErrorKind.UNKNOWN
