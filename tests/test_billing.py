"""接口权重与客户端自声明代价：上下限夹取与非法输入。"""

from __future__ import annotations

import logging

import pytest

from gateway import CostResolver, EndpointCostPolicy
from gateway.errors import BillingRejected, BillingRejectReason

logger = logging.getLogger("test.billing")


def make_resolver() -> CostResolver:
    return CostResolver(
        {
            "/heavy": EndpointCostPolicy(weight=5.0, min_declared=2.0, max_declared=10.0),
            "/floor": EndpointCostPolicy(weight=1.0, min_declared=1.0),
            "/fixed": EndpointCostPolicy(weight=3.0),  # 不开放自声明
        }
    )


def test_undeclared_cost_uses_endpoint_weight():
    r = make_resolver()
    for raw in (None, ""):
        got = r.resolve("/heavy", raw)
        logger.info("endpoint=/heavy declared=%r -> cost=%s clamped=%s",
                    raw, got.cost, got.clamped)
        assert got.cost == 5.0 and got.clamped is False
    # 未知接口默认权重 1
    assert r.resolve("/nope", None).cost == 1.0


def test_declared_within_range_used_as_is():
    got = make_resolver().resolve("/heavy", "4.5")
    logger.info("declared='4.5' within [2,10] -> cost=%s", got.cost)
    assert (got.cost, got.declared, got.clamped) == (4.5, 4.5, False)


@pytest.mark.parametrize(
    "declared,expected",
    [
        (0.0, 2.0),       # 低于下限 -> 取下限
        ("1", 2.0),
        (999, 10.0),      # 高于上限 -> 取上限
        ("10.0", 10.0),   # 恰好上限：不算夹取
        ("2.0", 2.0),     # 恰好下限：不算夹取
    ],
)
def test_declared_out_of_range_is_clamped(declared, expected):
    got = make_resolver().resolve("/heavy", declared)
    logger.info("declared=%r -> cost=%s clamped=%s", declared, got.cost, got.clamped)
    assert got.cost == expected


def test_clamp_flag_is_exact_at_boundaries():
    r = make_resolver()
    assert r.resolve("/heavy", 2.0).clamped is False
    assert r.resolve("/heavy", 10.0).clamped is False
    assert r.resolve("/heavy", 1.999).clamped is True
    assert r.resolve("/heavy", 10.001).clamped is True


def test_only_floor_configured_no_upper_bound():
    got = make_resolver().resolve("/floor", "100000")
    logger.info("floor-only endpoint declared=100000 -> cost=%s", got.cost)
    assert got.cost == 100000.0 and got.clamped is False


def test_undeclarable_endpoint_silently_ignores_declaration():
    r = make_resolver()
    got = r.resolve("/fixed", 9)
    logger.info("fixed-weight endpoint declared=9 -> cost=%s clamped=%s", got.cost, got.clamped)
    assert got.cost == 3.0 and got.clamped is True
    # 声明恰好等于权重时不算被夹
    assert r.resolve("/fixed", 3).clamped is False


@pytest.mark.parametrize("bad", ["abc", "1.2.3", -1, -0.1, float("nan"), float("inf"), True])
def test_malformed_declaration_rejected_before_billing(bad):
    with pytest.raises(BillingRejected) as exc_info:
        make_resolver().resolve("/heavy", bad)
    logger.info("declared=%r -> reason=%s", bad, exc_info.value.reason)
    assert exc_info.value.billing_reason is BillingRejectReason.DECLARED_COST_MALFORMED
    assert exc_info.value.status_code == 400


def test_invalid_policy_bounds_rejected():
    with pytest.raises(ValueError):
        EndpointCostPolicy(weight=1.0, min_declared=10.0, max_declared=1.0)
