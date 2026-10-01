"""网关配置类型（纯数据定义）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QuotaRule:
    """单级限流规则：令牌桶。

    rate   稳态速率（令牌/秒）
    burst  桶容量，可容忍的最大突发
    """

    rate: float
    burst: float


@dataclass(frozen=True)
class QuotaConfig:
    """多级限流配置。

    client_rules: client_id -> 规则（客户端维度，聚合所有接口）
    api_rules:    api     -> 规则（接口维度，聚合所有客户端）
    ca_rules:     (client_id, api) -> 规则（客户端×接口维度，优先覆盖）
    """

    client_rules: dict[str, QuotaRule] = field(default_factory=dict)
    api_rules: dict[str, QuotaRule] = field(default_factory=dict)
    ca_rules: dict[tuple[str, str], QuotaRule] = field(default_factory=dict)
    default_client: QuotaRule | None = None
    default_api: QuotaRule | None = None
    state_path: str = "quota_state.json"
    flush_interval: float = 1.0


@dataclass(frozen=True)
class BreakerConfig:
    """熔断器配置。"""

    failure_threshold: int = 5
    open_duration: float = 5.0
    half_open_max_probes: int = 1
    success_threshold: int = 1
    timeout: float = 2.0


@dataclass(frozen=True)
class Settings:
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)


def default_settings() -> Settings:
    """内置默认设置（供演示与本地验证使用）。

    配额模型（令牌桶，rate=稳态令牌/秒，burst=桶容量/突发上限）：
    - 客户端维度：free(1/s, 突发2)，pro(10/s, 突发20)，未登记客户端 2/s 突发4
    - 接口维度：echo(5/s, 突发5)，data(2/s, 突发4)，未登记接口 20/s 突发20
    - 客户端×接口维度（最严格优先）：free 调 data 为 1/s 突发1
    """
    quota = QuotaConfig(
        client_rules={
            "free": QuotaRule(rate=1.0, burst=2.0),
            "pro": QuotaRule(rate=10.0, burst=20.0),
        },
        api_rules={
            "echo": QuotaRule(rate=5.0, burst=5.0),
            "data": QuotaRule(rate=2.0, burst=4.0),
        },
        ca_rules={
            ("free", "data"): QuotaRule(rate=1.0, burst=1.0),
        },
        default_client=QuotaRule(rate=2.0, burst=4.0),
        default_api=QuotaRule(rate=20.0, burst=20.0),
        state_path="data/quota_state.json",
        flush_interval=1.0,
    )
    return Settings(quota=quota, breaker=BreakerConfig())
