"""版本注册与协商测试：边界解析、缺省稳定、弃用提示、超范围拒绝。"""

import logging

import pytest

from app.errors import ErrorCode, GatewayError
from app.versioning.negotiator import HEADER, VersionNegotiator
from app.versioning.registry import (
    VersionInfo,
    VersionRegistry,
    VersionStatus,
)

logger = logging.getLogger("tests.version")


@pytest.fixture()
def negotiator() -> VersionNegotiator:
    registry = VersionRegistry()
    registry.register(VersionInfo("echo", 1, VersionStatus.DEPRECATED,
                                  sunset_date="2026-12-31", successor=2))
    registry.register(VersionInfo("echo", 2, VersionStatus.SUPPORTED))
    registry.register(VersionInfo("echo", 3, VersionStatus.REMOVED, successor=2))
    return VersionNegotiator(registry)


def test_missing_version_resolves_stably_to_latest(negotiator: VersionNegotiator) -> None:
    for raw in (None, "", "   "):
        result = negotiator.negotiate("echo", raw)
        logger.info("输入 %r -> 解析版本=%s explicit=%s",
                    raw, result.resolved.version, result.explicit)
        assert result.resolved.version == 2
        assert result.explicit is False


def test_supported_version_passes(negotiator: VersionNegotiator) -> None:
    result = negotiator.negotiate("echo", "2")
    logger.info("输入 '2' -> %s warnings=%s", result.resolved.version, result.warnings)
    assert result.resolved.version == 2
    assert result.warnings == []


def test_deprecated_version_passes_with_warning(negotiator: VersionNegotiator) -> None:
    result = negotiator.negotiate("echo", "1")
    logger.info("输入 '1' -> 状态=%s 提示=%s",
                result.resolved.status.value, result.warnings)
    assert result.resolved.status is VersionStatus.DEPRECATED
    assert len(result.warnings) == 1
    assert "2026-12-31" in result.warnings[0]
    assert "v2" in result.warnings[0]


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "1.5", "v2"])
def test_invalid_version_rejected(negotiator: VersionNegotiator, raw: str) -> None:
    with pytest.raises(GatewayError) as exc_info:
        negotiator.negotiate("echo", raw)
    err = exc_info.value
    logger.info("输入 %r -> 拒绝 %s/%d", raw, err.code.value, err.status_code)
    assert err.code is ErrorCode.VERSION_INVALID
    assert err.status_code == 400


@pytest.mark.parametrize("raw,successor", [("3", 2), ("99", None)])
def test_unsupported_version_rejected(negotiator: VersionNegotiator, raw: str,
                                      successor) -> None:
    with pytest.raises(GatewayError) as exc_info:
        negotiator.negotiate("echo", raw)
    err = exc_info.value
    logger.info("输入 %r -> 拒绝 %s/%d details=%s",
                raw, err.code.value, err.status_code, err.extra)
    assert err.code is ErrorCode.VERSION_UNSUPPORTED
    assert err.status_code == 426
    assert err.extra["supported"] == [1, 2]
    assert err.extra.get("successor") is successor
    assert err.extra["header"] == HEADER
