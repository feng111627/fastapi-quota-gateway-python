"""请求代价解析：接口权重 + 客户端自声明代价的合规处理。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import BillingRejected, BillingRejectReason


@dataclass(frozen=True)
class EndpointCostPolicy:
    """接口计费策略：基础权重与允许客户端自声明的代价区间。

    min_declared/max_declared 均为 None 表示该接口不接受自声明，一律按权重计费。
    """

    weight: float = 1.0
    min_declared: float | None = None
    max_declared: float | None = None

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError("接口权重不能为负")
        lo, hi = self.min_declared, self.max_declared
        if lo is not None and lo < 0:
            raise ValueError("min_declared 不能为负")
        if hi is not None and hi < 0:
            raise ValueError("max_declared 不能为负")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"自声明区间非法: min={lo} > max={hi}")


@dataclass(frozen=True)
class ResolvedCost:
    """一次请求最终计费的代价。"""

    cost: float
    declared: float | None
    clamped: bool  # 自声明是否被夹到允许区间边界/被忽略


def _parse_declared(raw: object) -> float | None:
    """把客户端声明解析成非负有限数值；None/空串视为未声明。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        # True/False 不是有意义的代价，直接按非法处理，避免隐式记成 1/0
        raise BillingRejected(
            BillingRejectReason.DECLARED_COST_MALFORMED,
            detail={"declared": str(raw)},
        )
    if isinstance(raw, int):
        value = float(raw)
    elif isinstance(raw, float):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            raise BillingRejected(
                BillingRejectReason.DECLARED_COST_MALFORMED,
                detail={"declared": raw},
            ) from None
    else:
        raise BillingRejected(
            BillingRejectReason.DECLARED_COST_MALFORMED,
            detail={"declared": str(raw)},
        )
    if not math.isfinite(value) or value < 0:
        raise BillingRejected(
            BillingRejectReason.DECLARED_COST_MALFORMED,
            detail={"declared": raw},
        )
    return value


class CostResolver:
    """按接口策略解析请求代价。

    明确规矩（文档化、可复现）：

    - 未声明（None/空串）：代价 = 接口权重（未知接口权重为 1）。
    - 接口允许自声明（配置了 [min, max] 任意一端）：
      声明值夹到区间内——低于下限取下限，高于上限取上限，clamped=True。
      只配一端时，另一端分别按 0 / +inf 处理。
    - 接口不允许自声明：忽略声明，回落到接口权重，clamped=True。
    - 声明无法解析为非负有限数值：抛 declared_cost_malformed（HTTP 400），
      请求根本不进入计费。
    """

    def __init__(self, policies: dict[str, EndpointCostPolicy] | None = None) -> None:
        self._policies = dict(policies or {})

    def policy_for(self, endpoint: str) -> EndpointCostPolicy:
        return self._policies.get(endpoint, EndpointCostPolicy())

    def resolve(self, endpoint: str, declared_raw: object) -> ResolvedCost:
        policy = self.policy_for(endpoint)
        declared = _parse_declared(declared_raw)
        if declared is None:
            return ResolvedCost(cost=float(policy.weight), declared=None, clamped=False)

        lo = policy.min_declared
        hi = policy.max_declared
        if lo is None and hi is None:
            # 该接口不开放自声明：按权重计费，并显式告诉客户端声明被忽略
            return ResolvedCost(
                cost=float(policy.weight), declared=declared,
                clamped=declared != float(policy.weight),
            )

        lower = lo if lo is not None else 0.0
        effective = declared
        if effective < lower:
            effective = lower
        if hi is not None and effective > hi:
            effective = hi
        return ResolvedCost(
            cost=float(effective), declared=declared, clamped=effective != declared
        )
