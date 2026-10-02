"""计费闸门：分档令牌桶 + 周期总量账本 + 请求幂等记录。

所有跨结构操作（幂等登记、速率预占、总量预占、退还、视图查询）都在同一把
计费锁内完成，保证：
- 并发下额度不超发；
- 同一 request_id 的并发重复请求只有一个真正执行，其余得到 409；
- 已完成请求（成功/失败已退）用同一 request_id 重放同一结论，不再扣费；
- 速率桶、总量账本、幂等记录一起快照，重启后账目接得上。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .errors import (
    BillingRejected,
    BillingRejectReason,
    QuotaExhausted,
    QuotaReason,
    RateLimitExceeded,
    RateLimitReason,
)
from .plans import Plan, PlanRegistry
from .quota import QuotaLedger, QuotaStatusView
from .ratelimit import TokenBucket

# 幂等记录状态
_IN_FLIGHT = "in_flight"
_COMMITTED = "committed"  # 成功并计费
_REFUNDED = "refunded"  # 上游失败，已退还


@dataclass(frozen=True)
class QuotaView:
    """对客户端暴露的额度视图。"""

    plan: str
    rate_remaining: float
    rate_capacity: float
    quota: QuotaStatusView

    def to_dict(self) -> dict:
        q = self.quota
        return {
            "plan": self.plan,
            "rate_remaining": round(self.rate_remaining, 3),
            "rate_capacity": self.rate_capacity,
            "quota_limit": q.limit,
            "quota_used": round(q.used, 3),
            "quota_remaining": None if q.remaining == float("inf") else round(q.remaining, 3),
            "quota_reset_at": q.reset_at,
            "quota_reset_in_sec": q.reset_in_sec,
        }


@dataclass(frozen=True)
class ReplayDirective:
    """同一 request_id 已有终态结论：直接重放，不再扣费、不再触达上游。"""

    success: bool
    body: object = None
    error_kind: str | None = None  # timeout / connection / business / circuit_open
    error_message: str = ""
    replayed: bool = True


@dataclass
class _IdemRecord:
    client_id: str
    cost: float
    state: str
    success: bool = False
    body: object = None
    error_kind: str | None = None
    error_message: str = ""

    def directive(self) -> ReplayDirective:
        return ReplayDirective(
            success=self.success,
            body=self.body,
            error_kind=self.error_kind,
            error_message=self.error_message,
        )


class AccountingGate:
    """统一计费闸门。"""

    def __init__(
        self,
        plans: PlanRegistry,
        *,
        state_path: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._plans = plans
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}
        self._ledger = QuotaLedger(clock=self._clock)
        self._idem: dict[str, _IdemRecord] = {}
        self._state_path = state_path
        if state_path:
            self._load(state_path)

    # ------------------------------------------------------------------ 查询

    def view(self, client_id: str) -> QuotaView:
        now = self._clock()
        with self._lock:
            plan = self._plans.plan_for(client_id)
            bucket = self._adapted_bucket(client_id, plan, now)
            return QuotaView(
                plan=plan.name,
                rate_remaining=bucket.tokens,
                rate_capacity=bucket.capacity,
                quota=self._ledger.status(client_id, plan, now),
            )

    # ------------------------------------------------- 请求计费生命周期入口

    def begin_request(
        self, client_id: str, request_id: str | None, cost: float
    ) -> ReplayDirective | None:
        """登记幂等键并预占速率 + 总量额度。

        - 无 request_id：不做幂等，只预占。
        - request_id 已有终态：返回重放指令（不扣费、不触达上游）。
        - request_id 正被另一请求持有：抛 request_in_flight（HTTP 409）。
        - 预占失败（速率/总量）：抛对应错误，并回收幂等登记。
        """
        now = self._clock()
        with self._lock:
            plan = self._plans.plan_for(client_id)

            if request_id is not None:
                existing = self._idem.get(request_id)
                if existing is not None:
                    if existing.client_id != client_id:
                        raise BillingRejected(
                            BillingRejectReason.REQUEST_IN_FLIGHT,
                            detail={"request_id": request_id, "conflict": "client_mismatch"},
                        )
                    if existing.state == _IN_FLIGHT:
                        raise BillingRejected(
                            BillingRejectReason.REQUEST_IN_FLIGHT,
                            detail={"request_id": request_id},
                        )
                    return existing.directive()  # committed / refunded：重放
                self._idem[request_id] = _IdemRecord(
                    client_id=client_id, cost=cost, state=_IN_FLIGHT
                )

            try:
                self._reserve_locked(client_id, plan, cost, now)
            except BaseException:
                # 预占失败：请求未被受理，幂等登记不能残留为 in_flight
                if request_id is not None:
                    self._idem.pop(request_id, None)
                raise
            return None

    def commit(self, client_id: str, request_id: str | None, cost: float, body: object) -> None:
        """上游成功：计费落定，记录可重放的成功结论。"""
        with self._lock:
            if request_id is not None:
                rec = self._idem.get(request_id)
                if rec is not None:
                    rec.state = _COMMITTED
                    rec.success = True
                    rec.body = body

    def fail_refund(
        self,
        client_id: str,
        request_id: str | None,
        cost: float,
        *,
        error_kind: str,
        error_message: str,
    ) -> None:
        """上游失败（超时/连接/熔断）：退还速率与总量，记录可重放的失败结论。

        每个预占只退还一次；幂等终态下重放的失败不会再走这里（无费可退）。
        """
        now = self._clock()
        with self._lock:
            self._refund_locked(client_id, cost, now)
            if request_id is not None:
                rec = self._idem.get(request_id)
                if rec is not None:
                    rec.state = _REFUNDED
                    rec.success = False
                    rec.error_kind = error_kind
                    rec.error_message = error_message

    def mark_terminal(
        self,
        request_id: str,
        *,
        success: bool,
        body: object = None,
        error_kind: str | None = None,
        error_message: str = "",
    ) -> None:
        """把幂等记录固化为终态（不改变已扣额度）。用于业务失败：计费成立但结论为失败。"""
        with self._lock:
            rec = self._idem.get(request_id)
            if rec is None:
                return
            rec.state = _COMMITTED
            rec.success = success
            rec.body = body
            rec.error_kind = error_kind
            rec.error_message = error_message

    def abort(self, client_id: str, request_id: str | None, cost: float) -> None:
        """未触达上游前放弃（如版本拒绝）：全额回滚并清除幂等登记。

        语义上等同"这个请求从未发生"：同一 request_id 可被重新使用。
        """
        now = self._clock()
        with self._lock:
            self._refund_locked(client_id, cost, now)
            if request_id is not None:
                self._idem.pop(request_id, None)

    # ------------------------------------------------------------ 锁内原语

    def _adapted_bucket(self, client_id: str, plan: Plan, now: float) -> TokenBucket:
        """取客户速率桶；套餐调整后参数即时生效（保留剩余令牌但不超过新突发）。"""
        bucket = self._buckets.get(client_id)
        if bucket is None:
            bucket = TokenBucket(
                capacity=float(plan.burst), rate_per_sec=plan.rate_per_sec
            )
            self._buckets[client_id] = bucket
            return bucket
        if bucket.capacity != float(plan.burst) or bucket.rate_per_sec != plan.rate_per_sec:
            bucket._refill(now)
            bucket.rate_per_sec = plan.rate_per_sec
            bucket.capacity = float(plan.burst)
            bucket.tokens = min(bucket.tokens, bucket.capacity)
        return bucket

    def _reserve_locked(self, client_id: str, plan: Plan, cost: float, now: float) -> None:
        bucket = self._adapted_bucket(client_id, plan, now)
        before = (bucket.tokens, bucket.last_refill)
        rate_ok, wait = bucket.try_consume(now, cost)
        if not rate_ok:
            bucket.tokens, bucket.last_refill = before
            raise RateLimitExceeded(
                RateLimitReason.CLIENT_RATE_EXCEEDED,
                retry_after=round(wait, 3),
                detail=self._reject_detail(client_id, plan, now),
            )

        quota_ok, view = self._ledger.try_consume(client_id, plan, cost, now)
        if not quota_ok:
            # 回滚速率桶预占，保证"速度太快"和"额度用完"互不串账
            bucket.tokens, bucket.last_refill = before
            raise QuotaExhausted(
                QuotaReason.QUOTA_EXHAUSTED,
                retry_after=round(view.reset_in_sec or 0.0, 3),
                detail=self._reject_detail(client_id, plan, now, quota_view=view),
            )

    def _refund_locked(self, client_id: str, cost: float, now: float) -> None:
        plan = self._plans.plan_for(client_id)
        bucket = self._buckets.get(client_id)
        if bucket is not None:
            bucket._refill(now)
            bucket.tokens = min(bucket.capacity, bucket.tokens + cost)
        self._ledger.refund(client_id, plan, cost, now)

    def _reject_detail(
        self, client_id: str, plan: Plan, now: float, *, quota_view: QuotaStatusView | None = None
    ) -> dict:
        bucket = self._buckets.get(client_id)
        qv = quota_view or self._ledger.status(client_id, plan, now)
        return {
            "client_id": client_id,
            "plan": plan.name,
            "rate_remaining": round(bucket.tokens if bucket else float(plan.burst), 3),
            "quota_remaining": (
                None if qv.remaining == float("inf") else round(qv.remaining, 3)
            ),
        }

    # -------------------------------------------------------------- 持久化

    def snapshot(self) -> None:
        if not self._state_path:
            return
        with self._lock:
            data = {
                "version": 2,
                "buckets": {k: b.to_snapshot() for k, b in self._buckets.items()},
                "ledger": self._ledger.to_snapshot(),
                "idempotency": {
                    k: {
                        "client_id": v.client_id,
                        "cost": v.cost,
                        "state": v.state,
                        "success": v.success,
                        "body": _jsonable(v.body),
                        "error_kind": v.error_kind,
                        "error_message": v.error_message,
                    }
                    for k, v in self._idem.items()
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
        for key, raw in (data.get("buckets") or {}).items():
            try:
                self._buckets[key] = TokenBucket.from_snapshot(raw)
            except (KeyError, TypeError, ValueError):
                continue
        self._ledger.load_snapshot(data.get("ledger") or {})
        for key, raw in (data.get("idempotency") or {}).items():
            try:
                state = raw["state"]
            except (KeyError, TypeError):
                continue
            if state == _IN_FLIGHT:
                # 进程在请求执行中途崩溃：结论未知，不能永久占用 request_id。
                # 已扣额度保留在桶与账本中（"用掉的额度接得上"），仅解锁幂等键，
                # 允许客户端用同一编号重新发起（重新计费一次）。
                continue
            try:
                self._idem[key] = _IdemRecord(
                    client_id=raw["client_id"],
                    cost=float(raw.get("cost", 0.0)),
                    state=state,
                    success=bool(raw.get("success", False)),
                    body=raw.get("body"),
                    error_kind=raw.get("error_kind"),
                    error_message=raw.get("error_message", ""),
                )
            except (KeyError, TypeError, ValueError):
                continue


def _jsonable(value: object) -> object:
    """把成功结论转成可 JSON 持久化的形式；不可序列化时退化为类型标记。"""
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"__non_serializable__": type(value).__name__}
