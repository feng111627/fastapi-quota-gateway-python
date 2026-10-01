"""多版本注册表：版本共存、当前/弃用/淘汰状态。

约定：
- 版本号为正整数，同一 api 下多个版本可共存；
- SUPPORTED：当前可用；DEPRECATED：弃用期内仍可用但必须提示；
- REMOVED：已超出支持范围，注册信息仅用于给出明确拒绝与替代版本；
- latest(api) 返回“仍可用（未 REMOVED）的最大版本号”，
  作为未显式指定版本时的稳定缺省值。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from app.errors import ErrorCode, GatewayError


class VersionStatus(str, Enum):
    SUPPORTED = "supported"
    DEPRECATED = "deprecated"
    REMOVED = "removed"


@dataclass(frozen=True)
class VersionInfo:
    api: str
    version: int
    status: VersionStatus
    sunset_date: str | None = None
    successor: int | None = None


class VersionRegistry:
    def __init__(self) -> None:
        self._by_api: dict[str, dict[int, VersionInfo]] = defaultdict(dict)

    def register(self, info: VersionInfo) -> None:
        if info.version <= 0:
            raise ValueError(f"版本号必须为正整数: {info.version}")
        if info.version in self._by_api[info.api]:
            raise ValueError(f"重复注册: api={info.api} version={info.version}")
        self._by_api[info.api][info.version] = info

    def get(self, api: str, version: int) -> VersionInfo | None:
        return self._by_api.get(api, {}).get(version)

    def latest(self, api: str) -> int:
        usable = [v for v, info in self._by_api.get(api, {}).items()
                  if info.status is not VersionStatus.REMOVED]
        if not usable:
            raise GatewayError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"接口 {api} 没有任何可用版本",
                status_code=426,
            )
        return max(usable)

    def versions_of(self, api: str) -> list[VersionInfo]:
        return [self._by_api[api][v] for v in sorted(self._by_api[api])]
