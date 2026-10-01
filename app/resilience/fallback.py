"""降级策略：熔断打开或上游持续失败时返回可解释的兜底结果。

兜底结果必须显式携带 degraded=True 与具体原因码，
调用方据此区分“真实成功”与“降级成功”，保证结论可解释、可复现。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import ErrorCode


@dataclass(frozen=True)
class FallbackResult:
    payload: dict
    degraded: bool
    reason: ErrorCode


class FallbackPolicy:
    def fallback(self, api: str, code: ErrorCode) -> FallbackResult:
        return FallbackResult(
            payload={
                "api": api,
                "degraded": True,
                "data": None,
                "message": f"服务暂时不可用，已降级（原因：{code.value}）",
            },
            degraded=True,
            reason=code,
        )
