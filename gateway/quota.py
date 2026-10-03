"""分档配额：套餐定义、周期窗口总额度、幂等扣减与退还、状态持久化。

- 套餐（QuotaPlan）：速率/突发参数（供限流器按客户分档）+ 周期内总额度。
- 窗口：按 UTC 自然日（DAILY）或固定周期秒数（ROLLING）滚动重置。
- 幂等：按 (client_id, request_id) 记账，同一编号重试不重复扣、不重复退；
  记录带窗口标记，跨窗口自动失效。
- 账本三态机（_LedgerEntry.state）：
  - IN_FLIGHT：已扣减、调用结果未定，只有这一态允许退还；
  - COMMITTED：上游成功或业务错误（照常计费），占用永久确定，
    后续同编号重试无论成败都不得改动这笔额度；
  - REFUNDED：可退失败已退还，同编号再来视为新一轮扣减。
  commit/refund 都只对 IN_FLIGHT 生效且各自只迁移一次，并发下幂等。
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


class LedgerState(enum.Enum):
    """账本条目状态机。"""

    IN_FLIGHT = "in_flight"  # 已扣减，上游结果未定（唯一可退还态）
    COMMITTED = "committed"  # 已成功完成（或业务错误照常计费），占用永久确定
    REFUNDED = "refunded"  # 可退失败已退还，同编号再来按新一轮扣减


@dataclass
class _LedgerEntry:
    client_id: str
    cost: float
    window_start: float
    state: LedgerState = LedgerState.IN_FLIGHT

    @property
    def refunded(self) -> bool:
        """兼容旧快照读取口径。"""
        return self.state is LedgerState.REFUNDED


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
    - charge/refund 在同一把锁内完成，并发下不超发、不串账。
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
                if entry is not None and entry.state is not LedgerState.REFUNDED:
                    # IN_FLIGHT（并发重试）或 COMMITTED（已成功）：
                    # 本次不重复扣，且后续 commit/refund 都不会再动这笔账
                    return ChargeResult(
                        allowed=True,
                        status=self._status_locked(client_id, plan, account),
                        cost=entry.cost,
                        duplicate=True,
                    )
                # state is REFUNDED（或无记录）时视为新一轮扣减（重试上一已退还的请求）

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

    def commit(self, client_id: str, request_id: str) -> bool:
        """把一笔在途扣减标记为 COMMITTED（请求已成功完成，照常计费）。

        只有 IN_FLIGHT 的当前窗口记录会迁移；COMMITTED / REFUNDED 均为 no-op。
        返回是否真正发生了状态迁移。提交后的占用不会再被任何同编号重试退掉。
        """
        key = f"{client_id}:{request_id}"
        now = self._clock()
        with self._lock:
            entry = self._ledger.get(key)
            if entry is None or entry.state is not LedgerState.IN_FLIGHT:
                return False
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)
            if account.window_start != entry.window_start:
                # 跨窗口的旧账：不迁移、不动新窗口账目
                return False
            entry.state = LedgerState.COMMITTED
            return True

    def refund(self, client_id: str, request_id: str) -> bool:
        """按 request_id 幂等退还；返回是否真正发生了退还。

        仅 IN_FLIGHT 的记录可退：COMMITTED（已成功）绝不退，
        REFUNDED 不重复退；并发退还下恰好一人生效。
        """
        key = f"{client_id}:{request_id}"
        now = self._clock()
        with self._lock:
            entry = self._ledger.get(key)
            if entry is None or entry.state is not LedgerState.IN_FLIGHT:
                return False
            plan = self._plans[self._client_plans.get(client_id, self._default_plan)]
            account = self._account_locked(client_id, plan, now)
            if account.window_start == entry.window_start:
                account.used = max(0.0, account.used - entry.cost)
            # 无论是否同窗口都标记已退：跨窗口不把旧账退到新窗口，但防止重复退
            entry.state = LedgerState.REFUNDED
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
                        "state": e.state.value,
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
        def _entry_state(v: dict) -> LedgerState:
            raw = v.get("state")
            if raw is not None:
                return LedgerState(raw)
            # 旧格式只有 refunded 布尔位：未退的在途记录按 COMMITTED 处理，
            # 重启后已成功的重试结论不被后续重试推翻（宁可保守不退）
            return LedgerState.REFUNDED if v.get("refunded") else LedgerState.COMMITTED

        self._ledger = {
            k: _LedgerEntry(
                client_id=v["client_id"],
                cost=float(v["cost"]),
                window_start=float(v["window_start"]),
                state=_entry_state(v),
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
