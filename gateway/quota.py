"""周期总量额度账本：滚动/按天重置，可快照恢复。

线程安全：本类不自带锁，所有方法都在 AccountingGate 的同一把计费锁内调用，
从而保证"速率预占 + 总量预占 + 幂等登记"跨结构的原子性。
时钟约定：clock 返回秒；DAILY 模式下要求为 Unix 纪元秒（生产用 time.time），
滚动模式对时钟参考点无要求（测试可注入假钟）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from .plans import Plan, QuotaResetMode

_DAY_SEC = 86400.0


@dataclass(frozen=True)
class QuotaStatusView:
    """额度对外视图：剩余、已用、上限与重置时间。"""

    limit: int
    used: float
    remaining: float
    reset_at: float | None  # 时钟绝对时间；无限额时为 None
    reset_in_sec: float | None


@dataclass
class _Entry:
    used: float
    window: float  # ROLLING: 窗口起点秒；DAILY: 当天序号 floor(now/86400)

    def to_snapshot(self) -> dict:
        return {"used": self.used, "window": self.window}

    @classmethod
    def from_snapshot(cls, data: dict) -> "_Entry":
        return cls(used=float(data["used"]), window=float(data["window"]))


def _day_index(now: float) -> float:
    return math.floor(now / _DAY_SEC)


class QuotaLedger:
    """按客户维护周期总量额度。"""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._entries: dict[str, _Entry] = {}

    # ---- 窗口判定 ----

    def _current_window(self, plan: Plan, now: float) -> float:
        if plan.reset_mode is QuotaResetMode.DAILY:
            return float(_day_index(now))
        return now  # 调用方在需要新开窗口时使用

    def _roll(self, entry: _Entry | None, plan: Plan, now: float) -> _Entry:
        """返回当前有效窗口对应的条目；窗口满期则清零重开。"""
        if plan.reset_mode is QuotaResetMode.DAILY:
            day = float(_day_index(now))
            if entry is None or entry.window != day:
                return _Entry(used=0.0, window=day)
            return entry

        # ROLLING：以窗口起点 + 周期为满期时刻；无记录时先不开账（惰性）
        if entry is None:
            return _Entry(used=0.0, window=now)
        if now - entry.window >= plan.quota_period_sec:
            return _Entry(used=0.0, window=now)
        return entry

    def _reset_moment(self, entry: _Entry | None, plan: Plan, now: float) -> tuple[float | None, float | None]:
        rolled = self._roll(entry, plan, now)
        if plan.reset_mode is QuotaResetMode.DAILY:
            reset_at = (rolled.window + 1) * _DAY_SEC
        else:
            reset_at = rolled.window + plan.quota_period_sec
        return reset_at, max(0.0, reset_at - now)

    # ---- 对外查询 ----

    def status(self, client_id: str, plan: Plan, now: float | None = None) -> QuotaStatusView:
        now = self._clock() if now is None else now
        entry = self._roll(self._entries.get(client_id), plan, now)
        reset_at, reset_in = self._reset_moment(self._entries.get(client_id), plan, now)
        if plan.quota_limit == 0:
            return QuotaStatusView(
                limit=0, used=entry.used, remaining=math.inf,
                reset_at=None, reset_in_sec=None,
            )
        remaining = max(0.0, float(plan.quota_limit) - entry.used)
        return QuotaStatusView(
            limit=plan.quota_limit, used=entry.used, remaining=remaining,
            reset_at=reset_at, reset_in_sec=reset_in,
        )

    # ---- 扣减 / 退还 ----

    def try_consume(
        self, client_id: str, plan: Plan, cost: float, now: float | None = None
    ) -> tuple[bool, QuotaStatusView]:
        """额度充足则扣减并返回 (True, 最新视图)；不足则不动账，返回 (False, 视图)。"""
        now = self._clock() if now is None else now
        entry = self._roll(self._entries.get(client_id), plan, now)

        if plan.quota_limit == 0:
            # 不限总量：仍记录用量供对账，但永远放行
            entry.used += cost
            self._entries[client_id] = entry
            return True, self.status(client_id, plan, now)

        if entry.used + cost > float(plan.quota_limit) + 1e-9:
            # 不足：不动账，仅返回当前视图（含重置时间）
            reset_at, reset_in = self._reset_moment(self._entries.get(client_id), plan, now)
            return False, QuotaStatusView(
                limit=plan.quota_limit,
                used=entry.used,
                remaining=max(0.0, float(plan.quota_limit) - entry.used),
                reset_at=reset_at,
                reset_in_sec=reset_in,
            )

        entry.used += cost
        self._entries[client_id] = entry
        return True, self.status(client_id, plan, now)

    def refund(self, client_id: str, plan: Plan, cost: float, now: float | None = None) -> float:
        """退还此前扣减的总量。窗口已滚动则不退（避免把旧周期额度灌进新周期）。

        返回实际退还量（供速率桶对账）。
        """
        now = self._clock() if now is None else now
        entry = self._entries.get(client_id)
        if entry is None:
            return 0.0
        # 校验仍处于同一窗口：_roll 会给出新窗口，比较窗口标记即可
        current = self._roll(entry, plan, now)
        if current is not entry:
            return 0.0
        refunded = min(cost, entry.used)
        entry.used = max(0.0, entry.used - cost)
        if entry.used <= 1e-12:
            # 退净后保留窗口标记（仍在周期内），便于视图持续给出重置时间
            entry.used = 0.0
        return refunded

    # ---- 持久化 ----

    def to_snapshot(self) -> dict:
        return {"clients": {k: v.to_snapshot() for k, v in self._entries.items()}}

    def load_snapshot(self, data: dict) -> None:
        entries = {}
        for key, raw in (data or {}).get("clients", {}).items():
            try:
                entries[key] = _Entry.from_snapshot(raw)
            except (KeyError, TypeError, ValueError):
                continue
        self._entries = entries
