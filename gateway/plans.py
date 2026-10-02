"""客户分档套餐：速率、突发与周期总量额度；默认档 + 单客户覆盖。"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass


class QuotaResetMode(enum.Enum):
    """周期总量的重置方式。"""

    ROLLING = "rolling"  # 滚动窗口：距上次窗口起点满一个周期即重置
    DAILY = "daily"  # 按 UTC 自然日重置


@dataclass(frozen=True)
class Plan:
    """一个套餐档：令牌桶速率/突发 + 周期总量上限。"""

    name: str
    rate_per_sec: float
    burst: int
    quota_limit: int  # 每周期总量额度；0 表示不限制
    quota_period_sec: float = 86400.0
    reset_mode: QuotaResetMode = QuotaResetMode.ROLLING

    def __post_init__(self) -> None:
        if self.rate_per_sec < 0:
            raise ValueError(f"plan {self.name!r}: rate_per_sec 不能为负")
        if self.burst < 0:
            raise ValueError(f"plan {self.name!r}: burst 不能为负")
        if self.quota_limit < 0:
            raise ValueError(f"plan {self.name!r}: quota_limit 不能为负")
        if self.quota_limit > 0 and self.quota_period_sec <= 0:
            raise ValueError(f"plan {self.name!r}: quota_period_sec 必须为正")


class PlanRegistry:
    """套餐注册表：按客户解析套餐，未单独配置的客户落到默认档。

    覆盖关系可在运行期由运营调整（set_override），调整即时生效；
    已在账的额度窗口与速率桶不在此处处理（见 AccountingGate）。
    """

    def __init__(self, default: Plan, overrides: dict[str, Plan] | None = None) -> None:
        self._default = default
        self._overrides: dict[str, Plan] = dict(overrides or {})
        self._lock = threading.Lock()

    def plan_for(self, client_id: str) -> Plan:
        with self._lock:
            return self._overrides.get(client_id, self._default)

    def set_override(self, client_id: str, plan: Plan) -> None:
        """运营按客户单独调整套餐（即时生效）。"""
        with self._lock:
            self._overrides[client_id] = plan

    def remove_override(self, client_id: str) -> None:
        """取消单客户配置，回落默认档。"""
        with self._lock:
            self._overrides.pop(client_id, None)

    @property
    def default(self) -> Plan:
        return self._default
