"""依赖装配：按默认配置构建网关各组件。

版本兼容策略（内置注册）：
- echo: v1 弃用（2026-12-31 停用，替代 v2），v2 当前支持，v0/v3 未注册/淘汰；
- data: v2 弃用，v3 当前支持。
未显式指定版本时稳定解析到“最新可用版本”。
"""

from __future__ import annotations

from app.config import Settings, default_settings
from app.gateway import Gateway
from app.resilience.fallback import FallbackPolicy
from app.resilience.upstream import HTTPUpstreamClient
from app.quota.limiter import MultiLevelLimiter
from app.quota.store import QuotaStore
from app.versioning.negotiator import VersionNegotiator
from app.versioning.registry import VersionInfo, VersionRegistry, VersionStatus


def build_registry() -> VersionRegistry:
    registry = VersionRegistry()
    registry.register(VersionInfo("echo", 1, VersionStatus.DEPRECATED,
                                  sunset_date="2026-12-31", successor=2))
    registry.register(VersionInfo("echo", 2, VersionStatus.SUPPORTED))
    registry.register(VersionInfo("echo", 3, VersionStatus.REMOVED, successor=2))
    registry.register(VersionInfo("data", 2, VersionStatus.DEPRECATED,
                                  sunset_date="2026-12-31", successor=3))
    registry.register(VersionInfo("data", 3, VersionStatus.SUPPORTED))
    return registry


def build_gateway(settings: Settings | None = None) -> Gateway:
    settings = settings or default_settings()
    store = QuotaStore(settings.quota.state_path)
    limiter = MultiLevelLimiter(settings.quota, store)
    negotiator = VersionNegotiator(build_registry())
    upstream = HTTPUpstreamClient("http://127.0.0.1:9000",
                                  timeout=settings.breaker.timeout)
    return Gateway(
        settings=settings,
        limiter=limiter,
        negotiator=negotiator,
        breakers={},
        upstream=upstream,
        fallback=FallbackPolicy(),
    )
