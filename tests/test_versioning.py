"""版本边界与协商测试。"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from gateway import (
    ApiVersion,
    VersionRegistry,
    VersionRejected,
    VersionRejectReason,
    VersionRule,
    VersionStatus,
)

logger = logging.getLogger("test.versioning")

TODAY = date(2026, 6, 1)


def make_registry() -> VersionRegistry:
    return VersionRegistry(
        rules=[
            VersionRule(ApiVersion(1, 0),
                        deprecated_since=date(2026, 1, 1),
                        sunset_on=date(2026, 12, 31)),
            VersionRule(ApiVersion(1, 1),
                        deprecated_since=date(2025, 1, 1),
                        sunset_on=date(2026, 3, 1)),  # 已下线
            VersionRule(ApiVersion(2, 0)),
        ],
        today=TODAY,
    )


def test_default_resolution_is_stable_and_current():
    reg = make_registry()
    r1 = reg.negotiate(None)
    r2 = reg.negotiate(None)
    logger.info("requested=None -> version=%s status=%s", r1.version, r1.status)
    assert r1.version == ApiVersion(2, 0)
    assert r1.status is VersionStatus.CURRENT
    assert r1 == r2  # 结果稳定可预期


def test_deprecated_version_usable_with_warning():
    reg = make_registry()
    r = reg.negotiate("v1")
    logger.info("requested=v1 -> version=%s status=%s warning=%s", r.version, r.status, r.warning)
    assert r.version == ApiVersion(1, 0)
    assert r.status is VersionStatus.DEPRECATED
    assert r.warning and "弃用" in r.warning and "2026-12-31" in r.warning


def test_sunset_version_rejected_as_too_old():
    reg = make_registry()
    with pytest.raises(VersionRejected) as exc_info:
        reg.negotiate("v1.1")
    logger.info("requested=v1.1 -> reason=%s detail=%s", exc_info.value.reason, exc_info.value.detail)
    assert exc_info.value.version_reason is VersionRejectReason.VERSION_TOO_OLD


def test_unknown_newer_version_rejected_as_too_new():
    reg = make_registry()
    with pytest.raises(VersionRejected) as exc_info:
        reg.negotiate("v9")
    logger.info("requested=v9 -> reason=%s", exc_info.value.reason)
    assert exc_info.value.version_reason is VersionRejectReason.VERSION_TOO_NEW


def test_malformed_version_rejected():
    reg = make_registry()
    for bad in ["abc", "v1.2.3", "", "v-1"]:
        with pytest.raises(VersionRejected) as exc_info:
            reg.negotiate(bad)
        logger.info("requested=%r -> reason=%s", bad, exc_info.value.reason)
        assert exc_info.value.version_reason is VersionRejectReason.VERSION_MALFORMED


def test_boundary_day_of_sunset_is_rejected():
    """sunset_on 当天即视为下线（边界值）。"""
    reg = VersionRegistry(
        rules=[
            VersionRule(ApiVersion(1, 0), deprecated_since=date(2026, 1, 1),
                        sunset_on=date(2026, 6, 1)),
            VersionRule(ApiVersion(2, 0)),
        ],
        today=TODAY,
    )
    assert reg.status_of(ApiVersion(1, 0)) is VersionStatus.SUNSET
    with pytest.raises(VersionRejected):
        reg.negotiate("v1")
    logger.info("requested=v1 on sunset day -> rejected as too_old")


def test_boundary_day_before_sunset_still_deprecated():
    reg = VersionRegistry(
        rules=[
            VersionRule(ApiVersion(1, 0), deprecated_since=date(2026, 1, 1),
                        sunset_on=date(2026, 6, 1)),
            VersionRule(ApiVersion(2, 0)),
        ],
        today=date(2026, 5, 31),
    )
    r = reg.negotiate("v1")
    logger.info("requested=v1 day before sunset -> status=%s", r.status)
    assert r.status is VersionStatus.DEPRECATED
