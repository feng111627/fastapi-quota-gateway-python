"""版本协商：显式指定优先，缺省稳定解析到默认（最新可用）版本。

解析规则（结果稳定、可预期）：
1. 头缺失/空白         -> 解析为该 api 的 latest（最新可用版本），explicit=False；
2. 非正整数/非数字     -> 400 version.invalid；
3. 正整数但未注册      -> 426 version.unsupported（附支持版本清单）；
4. 已 REMOVED          -> 426 version.unsupported（指向 successor）；
5. DEPRECATED          -> 放行，带 Warning 响应头与 sunset 提示；
6. SUPPORTED           -> 放行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.errors import ErrorCode, GatewayError
from app.versioning.registry import VersionInfo, VersionRegistry, VersionStatus

logger = logging.getLogger("app.versioning")

HEADER = "X-API-Version"


@dataclass(frozen=True)
class Negotiation:
    api: str
    requested: int | None
    resolved: VersionInfo
    explicit: bool
    warnings: list[str] = field(default_factory=list)


class VersionNegotiator:
    def __init__(self, registry: VersionRegistry) -> None:
        self.registry = registry

    def negotiate(self, api: str, raw: str | None) -> Negotiation:
        warnings: list[str] = []
        explicit = raw is not None and raw.strip() != ""
        if not explicit:
            version = self.registry.latest(api)
            info = self.registry.get(api, version)
            assert info is not None
            logger.info("版本协商 api=%s 请求头缺失 -> 稳定缺省 v%d", api, version)
            return Negotiation(api, None, info, False, warnings)

        text = raw.strip()  # type: ignore[union-attr]
        if not text.isdigit() or int(text) <= 0:
            logger.info("版本协商 api=%s 非法输入=%r -> version.invalid", api, text)
            raise GatewayError(
                ErrorCode.VERSION_INVALID,
                f"非法的 {HEADER}: {text!r}，应为正整数",
                status_code=400,
                extra={"header": HEADER, "input": text},
            )
        requested = int(text)
        info = self.registry.get(api, requested)

        if info is None:
            supported = [v.version for v in self.registry.versions_of(api)
                         if v.status is not VersionStatus.REMOVED]
            logger.info("版本协商 api=%s 请求 v%d 未注册 -> unsupported, 支持=%s",
                        api, requested, supported)
            raise GatewayError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"接口 {api} 不支持版本 {requested}",
                status_code=426,
                extra={"supported": supported, "header": HEADER},
            )

        if info.status is VersionStatus.REMOVED:
            logger.info("版本协商 api=%s 请求 v%d 已淘汰 -> unsupported, 替代=%s",
                        api, requested, info.successor)
            raise GatewayError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"接口 {api} 的版本 {requested} 已停止支持",
                status_code=426,
                extra={"supported": [v.version for v in self.registry.versions_of(api)
                                     if v.status is not VersionStatus.REMOVED],
                       "successor": info.successor, "header": HEADER},
            )

        if info.status is VersionStatus.DEPRECATED:
            warning = (f"{api} v{requested} 已弃用"
                       f"{f'，计划于 {info.sunset_date} 停用' if info.sunset_date else ''}"
                       f"{f'，请迁移至 v{info.successor}' if info.successor else ''}")
            warnings.append(warning)
            logger.info("版本协商 api=%s 请求 v%d 弃用期放行，提示=%s",
                        api, requested, warning)
        else:
            logger.info("版本协商 api=%s 请求 v%d 支持，放行", api, requested)

        return Negotiation(api, requested, info, True, warnings)
