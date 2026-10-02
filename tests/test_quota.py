"""周期总量额度账本：分档总量边界、重置方式、退还与重启一致性。"""

from __future__ import annotations

import logging

from gateway import Plan, QuotaLedger, QuotaResetMode

logger = logging.getLogger("test.quota")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def rolling_plan(limit: int = 10, period: float = 100.0) -> Plan:
    return Plan("basic", rate_per_sec=1000.0, burst=1000, quota_limit=limit,
                quota_period_sec=period, reset_mode=QuotaResetMode.ROLLING)


def test_quota_boundary_exactly_limit_allowed_next_rejected():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=10)

    ok, view = ledger.try_consume("c", plan, 10)
    logger.info("consume 10 of 10 -> allowed=%s remaining=%s", ok, view.remaining)
    assert ok and view.remaining == 0

    ok, view = ledger.try_consume("c", plan, 0.001)
    logger.info("consume 0.001 more -> allowed=%s used=%s reset_in=%s",
                ok, view.used, view.reset_in_sec)
    assert not ok and view.remaining == 0 and view.used == 10
    # 拒绝不动账
    assert ledger.status("c", plan).used == 10


def test_rolling_window_resets_after_period_boundary():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=5, period=100.0)
    ledger.try_consume("c", plan, 5)

    clk.advance(99.999)
    ok, _ = ledger.try_consume("c", plan, 0.001)
    logger.info("just before window end -> allowed=%s", ok)
    assert not ok  # 差一点也不重置

    clk.advance(0.0011)
    ok, view = ledger.try_consume("c", plan, 3)
    logger.info("just after window end -> allowed=%s remaining=%s used=%s",
                ok, view.remaining, view.used)
    assert ok and view.remaining == 2 and view.used == 3


def test_daily_reset_at_utc_midnight():
    # day 10 的 23:00
    clk = FakeClock(86400.0 * 10 + 82800.0)
    ledger = QuotaLedger(clock=clk)
    plan = Plan("d", 1, 1, quota_limit=5, reset_mode=QuotaResetMode.DAILY)
    for _ in range(5):
        assert ledger.try_consume("x", plan, 1)[0]
    assert not ledger.try_consume("x", plan, 1)[0]
    view = ledger.status("x", plan)
    logger.info("23:00 used=%s reset_at=%s (expect midnight day11=%s)",
                view.used, view.reset_at, 86400.0 * 11)
    assert view.reset_at == 86400.0 * 11

    clk.now = 86400.0 * 11  # 次日 00:00:00 UTC
    ok, view = ledger.try_consume("x", plan, 2)
    logger.info("next UTC midnight -> allowed=%s used=%s remaining=%s",
                ok, view.used, view.remaining)
    assert ok and view.used == 2 and view.remaining == 3


def test_refund_returns_quota_within_same_window():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=5)
    ledger.try_consume("c", plan, 4)
    refunded = ledger.refund("c", plan, 3)
    assert refunded == 3
    view = ledger.status("c", plan)
    logger.info("refund 3 within window -> used=%s remaining=%s", view.used, view.remaining)
    assert view.used == 1 and view.remaining == 4
    # 退还不会让用量变负
    assert ledger.refund("c", plan, 10) == 1
    assert ledger.status("c", plan).used == 0


def test_refund_does_not_leak_into_next_window():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=5)
    ledger.try_consume("c", plan, 5)
    clk.advance(100.0)
    assert ledger.refund("c", plan, 5) == 0.0
    logger.info("refund after rollover -> 0; new window untouched used=%s",
                ledger.status("c", plan).used)
    ok, view = ledger.try_consume("c", plan, 5)
    assert ok and view.remaining == 0


def test_unlimited_quota_always_allows_but_still_meters():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=0)
    ok, view = ledger.try_consume("c", plan, 10**6)
    logger.info("unlimited quota -> allowed=%s remaining=%s used=%s",
                ok, view.remaining, view.used)
    assert ok and view.remaining == float("inf") and view.reset_at is None
    assert view.used == 10**6


def test_snapshot_restores_consumed_amount_after_restart():
    clk = FakeClock()
    ledger = QuotaLedger(clock=clk)
    plan = rolling_plan(limit=10, period=1000.0)
    for cost in (2.5, 3.5, 4.0):
        assert ledger.try_consume("c", plan, cost)[0]

    restored = QuotaLedger(clock=clk)
    restored.load_snapshot(ledger.to_snapshot())
    view = restored.status("c", plan)
    logger.info("after restart used=%s remaining=%s", view.used, view.remaining)
    assert view.used == 10.0 and view.remaining == 0
    assert not restored.try_consume("c", plan, 0.001)[0]
