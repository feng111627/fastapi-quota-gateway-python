"""客户分档套餐：默认档回落、单客户覆盖、参数校验。"""

from __future__ import annotations

import logging

import pytest

from gateway import Plan, PlanRegistry, QuotaResetMode

logger = logging.getLogger("test.plans")


def make_plans() -> PlanRegistry:
    basic = Plan("basic", rate_per_sec=5.0, burst=10, quota_limit=1000)
    pro = Plan(
        "pro", rate_per_sec=20.0, burst=40, quota_limit=20_000,
        quota_period_sec=3600.0, reset_mode=QuotaResetMode.DAILY,
    )
    return PlanRegistry(basic, {"acme": pro})


def test_unconfigured_client_falls_back_to_default_plan():
    reg = make_plans()
    plan = reg.plan_for("newcomer")
    logger.info("client=newcomer -> plan=%s rate=%s burst=%s quota=%s",
                plan.name, plan.rate_per_sec, plan.burst, plan.quota_limit)
    assert plan.name == "basic"
    assert (plan.rate_per_sec, plan.burst, plan.quota_limit) == (5.0, 10, 1000)


def test_configured_client_gets_override():
    reg = make_plans()
    plan = reg.plan_for("acme")
    logger.info("client=acme -> plan=%s reset=%s", plan.name, plan.reset_mode)
    assert plan.name == "pro"
    assert plan.reset_mode is QuotaResetMode.DAILY
    assert plan.quota_period_sec == 3600.0


def test_override_can_be_adjusted_at_runtime_and_reverted():
    reg = make_plans()
    vip = Plan("vip", rate_per_sec=100.0, burst=200, quota_limit=100_000)
    reg.set_override("acme", vip)
    assert reg.plan_for("acme").name == "vip"
    logger.info("after set_override client=acme -> %s", reg.plan_for("acme").name)
    reg.remove_override("acme")
    assert reg.plan_for("acme").name == "basic"
    logger.info("after remove_override client=acme -> %s", reg.plan_for("acme").name)


def test_overrides_do_not_affect_other_clients_or_default():
    reg = make_plans()
    reg.set_override("x", Plan("x", 1.0, 1, 1))
    assert reg.plan_for("y").name == "basic"
    assert reg.default.name == "basic"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate_per_sec": -1.0, "burst": 1, "quota_limit": 1},
        {"rate_per_sec": 1.0, "burst": -1, "quota_limit": 1},
        {"rate_per_sec": 1.0, "burst": 1, "quota_limit": -1},
        {"rate_per_sec": 1.0, "burst": 1, "quota_limit": 1, "quota_period_sec": 0.0},
    ],
)
def test_invalid_plan_parameters_rejected(kwargs):
    with pytest.raises(ValueError):
        Plan("bad", **kwargs)
    logger.info("invalid plan params %s -> ValueError", kwargs)
