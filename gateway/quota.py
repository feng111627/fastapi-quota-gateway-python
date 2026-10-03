"""分档配额：套餐定义、周期窗口总额度、幂等扣减与退还、状态持久化。

- 套餐（QuotaPlan）：速率/突发参数（供限流器按客户分档）+ 周期内总额度。
- 窗口：按 UTC 自然日（DAILY）或固定周期秒数（ROLLING）滚动重置。
- 幂等：按 (client_id, request_id) 记账，同一编号重试不重复扣、不重复退；
  记录带窗口标记，跨窗口自动失效。
- 账本三态：pending（已扣、结果未知）/ committed（上游已成功，终态）/
  refunded（可退失败已退，终态）。已 committed 的成功占用不会被后续同编号
  重试的失败退还；退还后重试若成功，commit 时补扣，保证成功不白用。
- 持久化：已用额度、窗口起点、账本与客档映射原子写入 JSON，重启后接得上。
"""

from __future__ import annotations

import enum
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable


class QuotaPeriod(enum.Enum):
    DAILY = "daily"  # 按 UTC 自然日重置
    ROLLING = "rolling"  # 按 period_seconds 为周期滚动重置


@dataclass(frozen=True)
class QuotaPlan:
    """套餐：速率上限 + 突发量 + 周期总额度。"""

    name: str
    rate_per_sec: float
    burst: int
    quota_limit: int
    period: QuotaPeriod = QuotaPeriod.DAILY
    period_seconds: float = 86400.0  # 仅 ROLLING 使用

    def window_length(self) -> float:
        return 86400.0 if self.period is QuotaPeriod.DAILY else self.period_seconds

    def window_start(self, now: float) -> float:
        length = self.window_length()
        return (now // length) * length


@dataclass
class QuotaAccount:
    """单客户当前窗口的账目（线程安全由外层 Manager 的锁保证）。"""

    used: float
    window_start: float


@dataclass(frozen=True)
class QuotaStatus:
    """对外暴露的额度视图。"""

    plan: str
    limit: int
    used: float
    remaining: float
    reset_at: float  # 当前窗口结束（重置）的 epoch 秒


@dataclass(frozen=True)
class ChargeResult:
    allowed: bool
    status: QuotaStatus
    cost: float
    duplicate: bool = False  # 同一 request_id 已扣过，本次未重复扣
    retry_after: float = 0.0  # 拒绝时：距窗口重置的秒数


@dataclass
class _LedgerEntry:
    client_id: str
    cost: float
    window_start: float
    refunded: bool = False
    committed: bool = False  # 上游已成功：终态，任何后续重试都不得改动该占用


@dataclass(frozen=True)
class CostResolution:
    cost: float  # 最终计费 = 接口权重 × 声明单位数（夹取后）
    weight: float
    declared: float  # 实际采用的声明单位数
    clamped: bool  # 声明超出允许范围被夹取


@dataclass(frozen=True)
class CostPolicy:
    """请求代价：接口权重 × 客户端声明单位数。

    声明超出 [min_declared, max_declared] 时按边界夹取（clamped=True）：
    不拒绝、不轻信，按夹取后的值计费——这是唯一的处理规矩。
    未声明时按 min_declared 计。
    """

    endpoint_weights: dict[str, float]
    default_weight: float = 1.0
    min_declared: float = 1.0
    max_declared: float = 10.0

    def resolve(self, endpoint: str, declared: float | None) -> CostResolution:
        weight = float(self.endpoint_weights.get(endpoint, self.default_weight))
        units = self.min_declared if declared is None else float(declared)
        clamped = not (self.min_declared <= units <= self.max_declared)
        units = min(max(units, self.min_declared), self.max_declared)
        return CostResolution(cost=weight * units, weight=weight, declared=units, clamped=clamped)


class TieredQuotaManager:
    """按客户分档的周期总额度管理器。

    - 未单独配置的客户落到 default_plan；set_client_plan 可单独调整。
    - charge/refund/commit 在同一把锁内完成，并发下不超发、不串账。
    - 上游成功后须调用 commit 落定占用：已成功的编号不会被后续同编号
      重试的失败退还（refund 对 committed 条目直接拒绝）。
    """

    def __init__(
        self,
        plans: dict[str, QuotaPlan],
        default_plan: str,
        *,
        client_plans: dict[str, str] | None = None,
        state_path: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if default_plan not in plans:
            raise ValueError(f"unknown default plan: {default_plan}")
        self._plans = dict(plans)
        self._default_plan = default_plan
        self._client_plans: dict[str, str] = {}
        self._state_path = state_path
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._accounts: dict[str, QuotaAccount] = {}
        self._ledger: dict[str, _LedgerEntry] = {}
        for client_id, plan_name in (client_plans or {}).items():
            self.set_client_plan(client_id, plan_name)
        if state_path:
            self._load(state_path)

    # ---- 客档管理 ----

    def set_client_plan(self, client_id: str, plan_name: str) -> None:
        if plan_name not in self._plans:
            raise ValueError(f"unknown plan: {plan_name}")
        with self._lock:
            self._client_plans[client_id] = plan_name

    def plan_for(self, client_id: str) -> QuotaPlan:
        with self._lock:
            name = self._client_plans.get(client_id, self._default_plan)
        return self._plans[name]

    # ---- 扣减与退还 ----

    def charge(self, client_id: str, cost: float, request_id: str | None = None) -> ChargeResult:
        """原子扣减 cost 点额度；同一 request_id 重复调用不重复扣。"""
        now = self._clock()
        with self._lock:
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)

            key = f"{client_id}:{request_id}" if request_id else None
            if key is not None:
                entry = self._ledger.get(key)
                if entry is not None and entry.window_start != account.window_start:
                    # 跨窗口的旧记录失效，按新请求处理
                    del self._ledger[key]
                    entry = None
                if entry is not None and not entry.refunded:
                    # pending（结果未知）或 committed（已成功）：重试不重复扣。
                    # committed 是终态，后续重试无论成败都不得改动该笔占用。
                    return ChargeResult(
                        allowed=True,
                        status=self._status_locked(client_id, plan, account),
                        cost=entry.cost,
                        duplicate=True,
                    )
                # entry.refunded 时视为新一轮扣减（重试上一已退还的请求）；
                # 若这轮最终成功，commit 时会以新条目覆盖为 committed。

            if account.used + cost > plan.quota_limit:
                retry_after = account.window_start + plan.window_length() - now
                return ChargeResult(
                    allowed=False,
                    status=self._status_locked(client_id, plan, account),
                    cost=cost,
                    retry_after=round(max(retry_after, 0.0), 3),
                )

            account.used += cost
            if key is not None:
                self._ledger[key] = _LedgerEntry(
                    client_id=client_id, cost=cost, window_start=account.window_start
                )
            return ChargeResult(
                allowed=True, status=self._status_locked(client_id, plan, account), cost=cost
            )

    def refund(self, client_id: str, request_id: str) -> bool:
        """按 request_id 幂等退还；返回是否真正发生了退还。

        只退还"已扣但尚未成功"的一笔（pending）：
        - 已 committed（该编号已有一笔上游成功）：拒绝退还，成功占用不被后续
          同编号重试改动；
        - 已 refunded：拒绝，保证同一笔只退一次；
        - 跨窗口的旧账不退还到新窗口，仅标记已退，防止重复退。
        """
        key = f"{client_id}:{request_id}"
        now = self._clock()
        with self._lock:
            entry = self._ledger.get(key)
            if entry is None or entry.refunded or entry.committed:
                return False
            entry.refunded = True
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)
            if account.window_start == entry.window_start:
                account.used = max(0.0, account.used - entry.cost)
            # 跨窗口的旧账不退还到新窗口，仅标记已退，防止重复退
            return True

    def commit(self, client_id: str, request_id: str) -> bool:
        """标记该编号的扣减对应上游已成功（终态，幂等）。

        - pending -> committed：正常成功，占用保留；
        - 已 committed：无操作，幂等返回；
        - 已 refunded（本次重试先赶上可退失败被退、之后又成功）：补扣这笔代价，
          并转回 committed——成功的请求不能白用，也不会留下可再退的口子。
        返回本次是否发生了状态落定（已 committed 的重复成功返回 False）。
        """
        key = f"{client_id}:{request_id}"
        now = self._clock()
        with self._lock:
            entry = self._ledger.get(key)
            if entry is None:
                return False
            if entry.committed:
                return False
            was_refunded = entry.refunded
            entry.refunded = False
            entry.committed = True
            if was_refunded:
                plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
                account = self._account_locked(client_id, plan, now)
                if account.window_start == entry.window_start:
                    # 同窗口：退还后重试成功，把额度补扣回来，成功不白用
                    account.used += entry.cost
            return True

    def refund_charge(self, client_id: str, cost: float) -> None:
        """无 request_id 的直接退还（同一调用路径内补偿，天然只发生一次）。"""
        now = self._clock()
        with self._lock:
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)
            account.used = max(0.0, account.used - cost)

    # ---- 查询 ----

    def status(self, client_id: str) -> QuotaStatus:
        now = self._clock()
        with self._lock:
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)
            return self._status_locked(client_id, plan, account)

    # ---- 持久化 ----

    def snapshot(self) -> None:
        """原子写入：客档映射 + 各客户账目 + 幂等账本。"""
        if not self._state_path:
            return
        with self._lock:
            data = {
                "client_plans": dict(self._client_plans),
                "accounts": {
                    k: {"used": a.used, "window_start": a.window_start}
                    for k, a in self._accounts.items()
                },
                "ledger": {
                    k: {
                        "client_id": e.client_id,
                        "cost": e.cost,
                        "window_start": e.window_start,
                        "refunded": e.refunded,
                        "committed": e.committed,
                    }
                    for k, e in self._ledger.items()
                },
            }
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._state_path)

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for client_id, plan_name in data.get("client_plans", {}).items():
            if plan_name in self._plans:
                self._client_plans[client_id] = plan_name
        self._accounts = {
            k: QuotaAccount(used=float(v["used"]), window_start=float(v["window_start"]))
            for k, v in data.get("accounts", {}).items()
        }
        self._ledger = {
            k: _LedgerEntry(
                client_id=v["client_id"],
                cost=float(v["cost"]),
                window_start=float(v["window_start"]),
                refunded=bool(v["refunded"]),
                # 旧快照无该字段时按 pending 处理，语义与升级前一致
                committed=bool(v.get("committed", False)),
            )
            for k, v in data.get("ledger", {}).items()
        }

    # ---- 内部（均在锁内调用） ----

    def _account_locked(self, client_id: str, plan: QuotaPlan, now: float) -> QuotaAccount:
        window_start = plan.window_start(now)
        account = self._accounts.get(client_id)
        if account is None:
            account = self._accounts[client_id] = QuotaAccount(used=0.0, window_start=window_start)
        elif account.window_start != window_start:
            # 窗口滚动：重置账目
            account.used = 0.0
            account.window_start = window_start
        return account

    @staticmethod
    def _status_locked(client_id: str, plan: QuotaPlan, account: QuotaAccount) -> QuotaStatus:
        return QuotaStatus(
            plan=plan.name,
            limit=plan.quota_limit,
            used=account.used,
            remaining=max(0.0, plan.quota_limit - account.used),
            reset_at=account.window_start + plan.window_length(),
        )
