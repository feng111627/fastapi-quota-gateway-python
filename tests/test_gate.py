"""计费闸门：拒绝原因区分、退还幂等、并发不串账、重启一致性。"""

from __future__ import annotations

import logging
import threading

import pytest

from gateway import (
    AccountingGate,
    Plan,
    PlanRegistry,
    QuotaResetMode,
)
from gateway.errors import (
    BillingRejected,
    BillingRejectReason,
    QuotaExhausted,
    RateLimitExceeded,
)

logger = logging.getLogger("test.gate")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(
    *,
    rate: float = 1000.0,
    burst: int = 1000,
    quota: int = 1000,
    period: float = 100.0,
    reset_mode: QuotaResetMode = QuotaResetMode.ROLLING,
    state_path: str | None = None,
    clk: FakeClock | None = None,
) -> tuple[AccountingGate, FakeClock]:
    clock = clk or FakeClock()
    plan = Plan("basic", rate, burst, quota, period, reset_mode)
    return AccountingGate(PlanRegistry(plan), state_path=state_path, clock=clock), clock


def test_rate_rejected_is_distinguishable_from_quota_exhausted():
    gate, _ = make_gate(rate=0.0001, burst=3, quota=1000)
    for i in range(3):
        assert gate.begin_request("c", None, 1.0) is None
    with pytest.raises(RateLimitExceeded) as exc:
        gate.begin_request("c", None, 1.0)
    logger.info("burst exhausted -> reason=%s retry_after=%s",
                exc.value.reason, exc.value.retry_after)
    assert exc.value.reason == "client_rate_exceeded"
    assert exc.value.retry_after > 0

    gate2, _ = make_gate(rate=1000.0, burst=1000, quota=3)
    for i in range(3):
        gate2.begin_request("c0", None, 1.0)
        gate2.commit("c0", None, 1.0, i)
    with pytest.raises(QuotaExhausted) as exc2:
        gate2.begin_request("c0", None, 1.0)
    logger.info("quota exhausted -> reason=%s retry_after=%s detail=%s",
                exc2.value.reason, exc2.value.retry_after, exc2.value.detail)
    assert exc2.value.reason == "quota_exhausted"
    assert exc2.value.status_code == 429
    assert exc2.value.detail["quota_remaining"] == 0


def test_quota_rejection_does_not_consume_rate_tokens():
    gate, _ = make_gate(rate=10.0, burst=10, quota=2)
    gate.begin_request("c", None, 2.0)
    gate.commit("c", None, 2.0, "a")
    with pytest.raises(QuotaExhausted):
        gate.begin_request("c", None, 1.0)
    view = gate.view("c")
    logger.info("after quota reject: rate_remaining=%s quota_used=%s",
                view.rate_remaining, view.quota.used)
    # 速率桶应回滚（只剩已提交那 2 个）
    assert view.rate_remaining == 8.0
    assert view.quota.used == 2.0


def test_fail_refund_returns_both_rate_and_quota_once():
    gate, _ = make_gate(rate=10.0, burst=10, quota=5)
    gate.begin_request("c", None, 3.0)
    gate.fail_refund("c", None, 3.0, error_kind="timeout", error_message="t")
    view = gate.view("c")
    logger.info("after refund rate_remaining=%s quota_used=%s",
                view.rate_remaining, view.quota.used)
    assert view.rate_remaining == 10.0
    assert view.quota.used == 0.0


def test_idempotent_success_replays_without_double_charge():
    gate, _ = make_gate(quota=10)
    assert gate.begin_request("c", "req-1", 2.0) is None
    gate.commit("c", "req-1", 2.0, {"k": 1})

    directive = gate.begin_request("c", "req-1", 2.0)
    logger.info("replay success -> %s", directive)
    assert directive is not None and directive.success and directive.body == {"k": 1}
    assert gate.view("c").quota.used == 2.0  # 没有第二次扣减


def test_idempotent_failure_replays_without_double_refund():
    gate, _ = make_gate(quota=10)
    gate.begin_request("c", "req-1", 2.0)
    gate.commit("c", "req-1", 2.0, "x")  # 已用 2
    gate.begin_request("c", "req-2", 3.0)
    gate.fail_refund("c", "req-2", 3.0, error_kind="timeout", error_message="t")

    replay = gate.begin_request("c", "req-2", 3.0)
    logger.info("replay failure -> success=%s kind=%s", replay.success, replay.error_kind)
    assert replay is not None and not replay.success and replay.error_kind == "timeout"
    assert gate.view("c").quota.used == 2.0  # 没有重复退还


def test_business_failure_terminal_is_charged_but_not_refunded():
    gate, _ = make_gate(quota=10)
    gate.begin_request("c", "req-b", 2.0)
    gate.mark_terminal("req-b", success=False, error_kind="business", error_message="500")
    view = gate.view("c")
    logger.info("business failure -> quota_used=%s", view.quota.used)
    assert view.quota.used == 2.0
    replay = gate.begin_request("c", "req-b", 2.0)
    assert replay is not None and not replay.success and replay.error_kind == "business"
    assert gate.view("c").quota.used == 2.0


def test_concurrent_same_request_id_only_one_proceeds():
    gate, _ = make_gate(quota=0)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            directive = gate.begin_request("c", "hot-key", 1.0)
            result = "proceed" if directive is None else "replay"
        except BillingRejected as exc:
            result = exc.reason
        with lock:
            outcomes.append(result)
        logger.info("thread=%d -> %s", tid, result)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("proceed") == 1
    assert outcomes.count(BillingRejectReason.REQUEST_IN_FLIGHT.value) == 19
    # 唯一在途的请求落定后，再来应得到成功重放而非重新执行
    gate.commit("c", "hot-key", 1.0, "done")
    directive = gate.begin_request("c", "hot-key", 1.0)
    assert directive is not None and directive.success and directive.body == "done"


def test_concurrent_charge_and_refund_never_over_issues():
    """多线程混合扣减与退还：最终用量 == 提交数（每次 1），且永远不超总额度。"""
    gate, _ = make_gate(rate=1000.0, burst=1000, quota=50)
    committed = {"n": 0}
    refunded = {"n": 0}
    refused = {"n": 0}
    lock = threading.Lock()

    def worker(tid: int) -> None:
        for rid in range(40):
            request_id = f"t{tid}-{rid}"
            try:
                gate.begin_request("shared", request_id, 1.0)
            except (QuotaExhausted, RateLimitExceeded):
                with lock:
                    refused["n"] += 1
                continue
            # 偶数线程模拟超时退还，奇数线程成功提交
            if tid % 2 == 0:
                gate.fail_refund("shared", request_id, 1.0,
                                 error_kind="timeout", error_message="t")
                with lock:
                    refunded["n"] += 1
            else:
                gate.commit("shared", request_id, 1.0, "ok")
                with lock:
                    committed["n"] += 1

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    view = gate.view("shared")
    logger.info("committed=%s refunded=%s refused=%s final_used=%s",
                committed["n"], refunded["n"], refused["n"], view.quota.used)
    # 账目恒等式：账本用量恰好等于"已提交未退还"的请求数
    assert abs(view.quota.used - committed["n"]) < 1e-9
    assert committed["n"] == 50  # 总额度恰好用满，没有超发
    assert refunded["n"] > 0 and refused["n"] >= 0


def test_restart_restores_buckets_ledger_and_terminal_replay(tmp_path):
    state = tmp_path / "gate.json"
    gate, clk = make_gate(rate=1.0, burst=10, quota=20, period=10000.0,
                          state_path=str(state))
    gate.begin_request("c", "r-ok", 4.0)
    gate.commit("c", "r-ok", 4.0, {"v": 1})
    gate.begin_request("c", "r-fail", 3.0)
    gate.fail_refund("c", "r-fail", 3.0, error_kind="connection", error_message="x")
    gate.snapshot()

    restored, _ = make_gate(rate=1.0, burst=10, quota=20, period=10000.0,
                            state_path=str(state), clk=clk)
    view = restored.view("c")
    logger.info("restarted view: rate_remaining=%s quota_used=%s",
                view.rate_remaining, view.quota.used)
    assert view.quota.used == 4.0  # 成功的 4 接上；失败的 3 已退
    # 成功请求重放同一结论
    d = restored.begin_request("c", "r-ok", 4.0)
    assert d is not None and d.success and d.body == {"v": 1}
    assert restored.view("c").quota.used == 4.0  # 重放不重复扣
    # 失败请求重放失败结论
    d2 = restored.begin_request("c", "r-fail", 3.0)
    assert d2 is not None and not d2.success and d2.error_kind == "connection"


def test_stale_inflight_request_id_is_unlocked_after_restart(tmp_path):
    state = tmp_path / "gate.json"
    gate, clk = make_gate(quota=20, period=10000.0, state_path=str(state))
    gate.begin_request("c", "crashed-req", 2.0)
    # 进程在中途崩溃（没有 commit / fail_refund）
    gate.snapshot()

    restored, _ = make_gate(quota=20, period=10000.0, state_path=str(state), clk=clk)
    # 已扣额度仍在账上，但 request_id 不再被锁
    assert restored.view("c").quota.used == 2.0
    assert restored.begin_request("c", "crashed-req", 1.0) is None
    logger.info("stale in-flight unlocked; charged amount preserved")


def test_plan_override_takes_effect_on_next_decision():
    clock = FakeClock()
    small = Plan("basic", 1.0, 5, quota_limit=10, quota_period_sec=100.0)
    registry = PlanRegistry(small)
    gate = AccountingGate(registry, clock=clock)
    gate.begin_request("c", None, 1.0)  # 初始化桶
    gate.abort("c", None, 1.0)

    big = Plan("big", 100.0, 50, quota_limit=500, quota_period_sec=100.0)
    registry.set_override("c", big)
    view = gate.view("c")
    logger.info("after override plan=%s capacity=%s quota_limit=%s",
                view.plan, view.rate_capacity, view.quota.limit)
    assert view.plan == "big"
    assert view.rate_capacity == 50.0 and view.quota.limit == 500
