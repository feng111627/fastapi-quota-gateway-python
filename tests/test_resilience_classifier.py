"""上游错误分类测试：超时/连接/业务必须可区分。"""

import logging

import httpx
import pytest

from app.errors import ErrorCode
from app.resilience.classifier import (
    UpstreamErrorKind,
    UpstreamStatusError,
    classify,
    kind_to_code,
)

logger = logging.getLogger("tests.classifier")


@pytest.mark.parametrize(
    "exc,kind,code",
    [
        (httpx.TimeoutException("read timed out"),
         UpstreamErrorKind.TIMEOUT, ErrorCode.UPSTREAM_TIMEOUT),
        (httpx.ConnectTimeout("connect timed out"),
         UpstreamErrorKind.TIMEOUT, ErrorCode.UPSTREAM_TIMEOUT),
        (httpx.ConnectError("connection refused"),
         UpstreamErrorKind.CONNECTION, ErrorCode.UPSTREAM_CONNECTION),
        (httpx.ReadError("reset"),
         UpstreamErrorKind.CONNECTION, ErrorCode.UPSTREAM_CONNECTION),
        (UpstreamStatusError(500, "boom"),
         UpstreamErrorKind.BUSINESS, ErrorCode.UPSTREAM_BUSINESS),
        (RuntimeError("unexpected"),
         UpstreamErrorKind.UNKNOWN, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_classify_distinguishes_causes(exc, kind, code) -> None:
    result = classify(exc)
    logger.info("输入异常=%s -> 分类=%s 错误码=%s",
                type(exc).__name__, result.value, kind_to_code(result).value)
    assert result is kind
    assert kind_to_code(result) is code
