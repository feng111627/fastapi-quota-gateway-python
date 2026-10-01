"""接口多版本共存与协商：弃用期、支持范围、默认版本解析。"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import date

from .errors import VersionRejected, VersionRejectReason

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?$")


class VersionStatus(enum.Enum):
    CURRENT = "current"
    DEPRECATED = "deprecated"  # 弃用期内仍可用
    SUNSET = "sunset"  # 已超出弃用期，不可用


@dataclass(frozen=True, order=True)
class ApiVersion:
    major: int
    minor: int = 0

    @classmethod
    def parse(cls, raw: str) -> "ApiVersion":
        """解析 'v1' / 'v1.2' / '1.2' 形式；非法输入抛 VersionRejected。"""
        m = _VERSION_RE.match(raw.strip())
        if not m:
            raise VersionRejected(VersionRejectReason.VERSION_MALFORMED, requested=raw, supported=[])
        return cls(major=int(m.group(1)), minor=int(m.group(2) or 0))

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}"


@dataclass(frozen=True)
class VersionRule:
    version: ApiVersion
    deprecated_since: date | None = None  # 进入弃用期的日期
    sunset_on: date | None = None  # 弃用期结束、彻底下线的日期


@dataclass(frozen=True)
class NegotiationResult:
    version: ApiVersion
    status: VersionStatus
    warning: str | None = None  # 弃用提示文案


class VersionRegistry:
    """接口版本注册表与协商器。"""

    def __init__(self, rules: list[VersionRule], *, today: date | None = None) -> None:
        if not rules:
            raise ValueError("at least one version rule is required")
        self._rules = sorted(rules, key=lambda r: r.version)
        self._today = today or date.today()

    def status_of(self, version: ApiVersion) -> VersionStatus:
        rule = self._find(version)
        if rule is None:
            return VersionStatus.SUNSET
        if rule.sunset_on is not None and self._today >= rule.sunset_on:
            return VersionStatus.SUNSET
        if rule.deprecated_since is not None and self._today >= rule.deprecated_since:
            return VersionStatus.DEPRECATED
        return VersionStatus.CURRENT

    def negotiate(self, requested: str | None) -> NegotiationResult:
        """协商版本。

        - requested 为 None：稳定地解析到当前最新的非弃用版本；
          若全部已弃用则取最新未下线版本。结果只依赖注册表与日期，可预期。
        - 弃用期内版本：可用，附 warning。
        - 超出支持范围（过旧已下线 / 过新不存在 / 格式非法）：抛 VersionRejected。
        """
        supported = [str(r.version) for r in self._rules]

        if requested is None:
            version = self._default_version()
            return NegotiationResult(version=version, status=self.status_of(version))

        try:
            version = ApiVersion.parse(requested)
        except VersionRejected as exc:
            exc.detail["supported"] = supported
            raise

        rule = self._find(version)
        if rule is None:
            reason = (
                VersionRejectReason.VERSION_TOO_OLD
                if version < self._rules[0].version
                else VersionRejectReason.VERSION_TOO_NEW
            )
            raise VersionRejected(reason, requested=requested, supported=supported)

        status = self.status_of(version)
        if status is VersionStatus.SUNSET:
            raise VersionRejected(
                VersionRejectReason.VERSION_TOO_OLD, requested=requested, supported=supported
            )

        warning = None
        if status is VersionStatus.DEPRECATED:
            sunset = rule.sunset_on.isoformat() if rule.sunset_on else "待定"
            warning = (
                f"API 版本 {version} 已于 {rule.deprecated_since.isoformat()} 弃用，"
                f"将于 {sunset} 停止服务，请迁移到 {self._default_version()}"
            )
        return NegotiationResult(version=version, status=status, warning=warning)

    def _find(self, version: ApiVersion) -> VersionRule | None:
        for rule in self._rules:
            if rule.version == version:
                return rule
        return None

    def _default_version(self) -> ApiVersion:
        """最新非弃用版本；全部弃用时取最新未下线版本。"""
        for rule in reversed(self._rules):
            if self.status_of(rule.version) is VersionStatus.CURRENT:
                return rule.version
        for rule in reversed(self._rules):
            if self.status_of(rule.version) is VersionStatus.DEPRECATED:
                return rule.version
        raise VersionRejected(
            VersionRejectReason.VERSION_TOO_OLD, requested="<default>", supported=[]
        )
