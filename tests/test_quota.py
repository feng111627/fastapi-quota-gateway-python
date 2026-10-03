"""分档配额：套餐分档、总量边界、窗口重置、权重夹取、幂等扣退、持久化。"""

from __future__ import annotations

import logging
import threading

import pytest

from gateway import CostPolicy, QuotaPeriod, QuotaPlan, TieredQuotaManager

logger = logging.getLogger("test.quota")

DAY = 86400.0
T0 = 1_760_000_000.0  # 固定的测试基准时间


def make_clock():
    state = {"now": T0}
    return state, lambda: state["now"]


def make_manager(**kwargs) -> TieredQuotaManager:
    plans = {
        "free": QuotaPlan("free", rate_per_sec=1.0, burst=5, quota_limit=10),
        "pro": QuotaPlan("pro", rate_per_sec=10.0, burst=50, quota_limit=1000),
    }
    return TieredQuotaManager(plans, "free", **kwargs)


def test_default_plan_fallback_and_per_client_override():
    state, clock = make_clock()
    m = make_manager(clock=clock)
    assert m.plan_for("nobody").name == "free"  # 未配置落默认档
    m.set_client_plan("vip", "pro")  # 运营单独调整
    assert m.plan_for("vip").name == "pro"
    assert m.plan_for("nobody").name == "free"
    logger.info("vip -> plan=%s, nobody -> plan=%s", m.plan_for("vip").name, m.plan_for("nobody").name)
    with pytest.raises(ValueError):
        m.set_client_plan("vip", "no-such-plan")


def test_quota_boundary_exact_limit_then_rejected():
    state, clock = make_clock()
    m = make_manager(clock=clock)  # free: limit=10
    r1 = m.charge("c1", 6.0, "r1")
    r2 = m.charge("c1", 4.0, "r2")  # 恰好到边界：允许
    r3 = m.charge("c1", 0.5, "r3")  # 超出：拒绝
    logger.info("charges: r1=%s(remain=%s) r2=%s(remain=%s) r3=%s retry_after=%s",
                r1.allowed, r1.status.remaining, r2.allowed, r2.status.remaining,
                r3.allowed, r3.retry_after)
    assert r1.allowed and r2.allowed
    assert r2.status.remaining == 0.0
    assert not r3.allowed
    assert r3.status.remaining == 0.0  # 拒绝不扣减
    assert r3.retry_after > 0


def test_daily_window_rolls_at_utc_midnight():
    state, clock = make_clock()
    m = make_manager(clock=clock)
    m.charge("c1", 10.0, "r1")
    assert not m.charge("c1", 1.0, "r2").allowed
    # 推进到下一个 UTC 自然日
    state["now"] = (T0 // DAY + 1) * DAY
    r = m.charge("c1", 10.0, "r3")
    logger.info("after midnight: allowed=%s remaining=%s", r.allowed, r.status.remaining)
    assert r.allowed
    assert r.status.remaining == 0.0


def test_rolling_period_window_resets():
    state, clock = make_clock()
    plans = {"hourly": QuotaPlan("hourly", 1.0, 5, quota_limit=5,
                                 period=QuotaPeriod.ROLLING, period_seconds=60.0)}
    m = TieredQuotaManager(plans, "hourly", clock=clock)
    m.charge("c1", 5.0, "r1")
    assert not m.charge("c1", 1.0, "r2").allowed
    state["now"] += 61.0  # 滚入下一周期
    r = m.charge("c1", 5.0, "r3")
    logger.info("rolling window: after reset allowed=%s", r.allowed)
    assert r.allowed


def test_cost_policy_weight_and_declared_clamping():
    policy = CostPolicy({"/heavy": 3.0}, default_weight=1.0, min_declared=1.0, max_declared=5.0)
    cases = [
        ("/heavy", None, 3.0, False),   # 未声明按 min_declared
        ("/heavy", 2.0, 6.0, False),    # 权重 × 声明
        ("/heavy", 999.0, 15.0, True),  # 超上限夹到 5
        ("/heavy", 0.0, 3.0, True),     # 低于下限夹到 1
        ("/other", 2.0, 2.0, False),    # 未配权重的接口用默认权重
    ]
    for endpoint, declared, expected_cost, expected_clamped in cases:
        r = policy.resolve(endpoint, declared)
        logger.info("endpoint=%s declared=%s -> cost=%s clamped=%s",
                    endpoint, declared, r.cost, r.clamped)
        assert r.cost == expected_cost
        assert r.clamped is expected_clamped


def test_idempotent_charge_same_request_id():
    state, clock = make_clock()
    m = make_manager(clock=clock)
    r1 = m.charge("c1", 4.0, "req-1")
    r2 = m.charge("c1", 4.0, "req-1")
    logger.info("duplicate charge: first=%s second duplicate=%s remaining=%s",
                r1.allowed, r2.duplicate, r2.status.remaining)
    assert r1.allowed and r2.allowed and r2.duplicate
    assert m.status("c1").used == 4.0  # 只扣一次


def test_refund_idempotent_and_retry_recharges():
    state, clock = make_clock()
    m = make_manager(clock=clock)
    m.charge("c1", 4.0, "req-1")
    assert m.refund("c1", "req-1") is True
    assert m.refund("c1", "req-1") is False  # 不重复退
    assert m.refund("c1", "unknown") is False
    assert m.status("c1").used == 0.0
    # 退还后同一编号重试：视为新一轮扣减
    r = m.charge("c1", 4.0, "req-1")
    logger.info("retry after refund: allowed=%s duplicate=%s used=%s",
                r.allowed, r.duplicate, m.status("c1").used)
    assert r.allowed and not r.duplicate
    assert m.status("c1").used == 4.0


def test_concurrent_charge_never_over_issues():
    state, clock = make_clock()
    m = make_manager(clock=clock)  # free: limit=10
    n_threads, per_thread = 8, 5  # 共 40 次扣 1，只有 10 次能成功
    results: list[bool] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        for i in range(per_thread):
            r = m.charge("shared", 1.0, f"req-{tid}-{i}")
            with lock:
                results.append(r.allowed)
            logger.info("thread=%d req=%d -> allowed=%s remaining=%s",
                        tid, i, r.allowed, r.status.remaining)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.info("total=%d allowed=%d expected=10", len(results), sum(results))
    assert sum(results) == 10
    assert m.status("shared").used == 10.0


def test_concurrent_retry_same_id_no_cross_accounting():
    """并发重试同一编号：只扣一次；并发退还：只退一次。"""
    state, clock = make_clock()
    m = make_manager(clock=clock)
    n = 16
    charge_dup: list[bool] = []
    refund_ok: list[bool] = []
    lock = threading.Lock()

    def charge_worker() -> None:
        r = m.charge("c1", 3.0, "req-x")
        with lock:
            charge_dup.append(r.duplicate)

    threads = [threading.Thread(target=charge_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.info("concurrent charge: duplicates=%d/%d used=%s",
                sum(charge_dup), n, m.status("c1").used)
    assert m.status("c1").used == 3.0  # 恰好扣一次

    def refund_worker() -> None:
        ok = m.refund("c1", "req-x")
        with lock:
            refund_ok.append(ok)

    threads = [threading.Thread(target=refund_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.info("concurrent refund: refunded=%d/%d used=%s",
                sum(refund_ok), n, m.status("c1").used)
    assert sum(refund_ok) == 1  # 恰好退一次
    assert m.status("c1").used == 0.0


def test_state_restored_after_restart(tmp_path):
    state, clock = make_clock()
    path = str(tmp_path / "quota.json")
    m = make_manager(clock=clock, state_path=path)
    m.set_client_plan("vip", "pro")
    m.charge("c1", 7.0, "req-1")
    m.charge("vip", 100.0, "req-2")
    m.snapshot()

    # 模拟进程重启：新实例从快照恢复
    m2 = make_manager(clock=clock, state_path=path)
    s1, s2 = m2.status("c1"), m2.status("vip")
    logger.info("after restart: c1 used=%s plan=%s, vip used=%s plan=%s",
                s1.used, s1.plan, s2.used, s2.plan)
    assert s1.used == 7.0 and s1.plan == "free"
    assert s2.used == 100.0 and s2.plan == "pro"  # 客档映射也恢复
    # 账本恢复：重启后同一编号仍不重复扣
    dup = m2.charge("c1", 7.0, "req-1")
    assert dup.duplicate
    assert m2.status("c1").used == 7.0


def test_committed_entry_cannot_be_refunded_by_retry():
    """已成功（committed）的编号：重试返回 duplicate，任何失败都退不掉该笔。"""
    state, clock = make_clock()
    m = make_manager(clock=clock)
    r1 = m.charge("c1", 4.0, "req-1")
    assert r1.allowed
    assert m.commit("c1", "req-1") is True  # 首笔上游成功，占用落定

    r2 = m.charge("c1", 4.0, "req-1")  # 同编号重试（哪怕重试本身失败）
    logger.info("retry after success: duplicate=%s used=%s", r2.duplicate, m.status("c1").used)
    assert r2.duplicate and r2.allowed

    # 重试赶上超时：拒绝退还，已成功的占用原样保留
    assert m.refund("c1", "req-1") is False
    # 重复 commit 也不产生新动作（并发成功重试互不干扰）
    assert m.commit("c1", "req-1") is False
    logger.info("refund after success -> refused, used=%s remaining=%s",
                m.status("c1").used, m.status("c1").remaining)
    assert m.status("c1").used == 4.0
    assert m.status("c1").remaining == 6.0


def test_refunded_entry_recharged_when_retry_succeeds():
    """先超时退还、后重试成功：commit 时补扣，成功的请求不白用。"""
    state, clock = make_clock()
    m = make_manager(clock=clock)
    m.charge("c1", 4.0, "req-1")
    assert m.refund("c1", "req-1") is True
    assert m.status("c1").used == 0.0

    r = m.charge("c1", 4.0, "req-1")  # 退还后的新一轮重试
    assert r.allowed and not r.duplicate
    committed = m.commit("c1", "req-1")  # 这轮成功
    s = m.status("c1")
    logger.info("retry succeeds -> committed=%s used=%s remaining=%s",
                committed, s.used, s.remaining)
    assert committed is True
    assert s.used == 4.0
    # 已落定，之后再来失败重试也退不掉
    assert m.refund("c1", "req-1") is False
    assert m.status("c1").used == 4.0


def test_committed_outcome_restored_after_restart(tmp_path):
    """进程重启后：已成功的编号仍是 committed，重试超时退不掉、不重复扣。"""
    state, clock = make_clock()
    path = str(tmp_path / "quota.json")
    m = make_manager(clock=clock, state_path=path)
    m.charge("c1", 4.0, "req-1")
    m.commit("c1", "req-1")
    m.charge("c1", 2.0, "req-pending")  # 已扣尚未成功的也在账本里
    m.snapshot()

    m2 = make_manager(clock=clock, state_path=path)
    logger.info("restart: used=%s", m2.status("c1").used)
    assert m2.status("c1").used == 6.0
    dup = m2.charge("c1", 4.0, "req-1")
    assert dup.duplicate  # 已成功编号重试不重复扣
    assert m2.refund("c1", "req-1") is False  # 成功占用跨重启仍受保护
    assert m2.status("c1").used == 6.0
    logger.info("restart retry timeout -> refund refused, used=%s", m2.status("c1").used)


def test_ledger_entry_expires_across_windows():
    """跨窗口后旧编号记录失效，同编号按新请求扣减。"""
    state, clock = make_clock()
    m = make_manager(clock=clock)
    m.charge("c1", 4.0, "req-1")
    state["now"] = (T0 // DAY + 1) * DAY  # 次日
    r = m.charge("c1", 4.0, "req-1")
    logger.info("next window same id: duplicate=%s used=%s", r.duplicate, m.status("c1").used)
    assert not r.duplicate
    assert m.status("c1").used == 4.0
