"""多级限流器测试：并发不超发、原因可区分、拒绝后回补、重启恢复。"""

import asyncio
import logging
import os

import pytest

from app.config import QuotaConfig, QuotaRule
from app.errors import ErrorCode
from app.quota.limiter import MultiLevelLimiter
from app.quota.store import QuotaStore

logger = logging.getLogger("tests.limiter")


def make_config(*, client=(100.0, 100.0), api=(100.0, 100.0), ca=None):
    return QuotaConfig(
        client_rules={"c": QuotaRule(*client)},
        api_rules={"a": QuotaRule(*api)},
        ca_rules={("c", "a"): QuotaRule(*ca)} if ca else {},
    )


def test_concurrent_requests_never_over_issue() -> None:
    """突发容量=5，50 个并发请求必须且只能放行 5 个。"""
    cfg = make_config(client=(0.0, 5.0), api=(0.0, 5.0))
    limiter = MultiLevelLimiter(cfg)

    async def scenario():
        return await asyncio.gather(*[limiter.check("c", "a") for _ in range(50)])

    results = asyncio.run(scenario())
    allowed = sum(1 for r in results if r.allowed)
    logger.info("输入 50并发 突发容量=5，实际放行=%d", allowed)
    assert allowed == 5


def test_rejection_reasons_distinguished_per_level() -> None:
    async def scenario():
        # 客户端维度先触顶
        cfg_client = make_config(client=(0.0, 2.0), api=(0.0, 10.0))
        lim = MultiLevelLimiter(cfg_client)
        await lim.check("c", "a")
        await lim.check("c", "a")
        r_client = await lim.check("c", "a")

        # 接口维度先触顶
        cfg_api = make_config(client=(0.0, 10.0), api=(0.0, 1.0))
        lim2 = MultiLevelLimiter(cfg_api)
        await lim2.check("c", "a")
        r_api = await lim2.check("c", "a")

        # 客户端×接口维度最严格，优先触发
        cfg_ca = make_config(client=(0.0, 10.0), api=(0.0, 10.0), ca=(0.0, 1.0))
        lim3 = MultiLevelLimiter(cfg_ca)
        await lim3.check("c", "a")
        r_ca = await lim3.check("c", "a")
        return r_client, r_api, r_ca

    r_client, r_api, r_ca = asyncio.run(scenario())
    logger.info("三级拒绝原因 client=%s api=%s client_api=%s",
                r_client.reason, r_api.reason, r_ca.reason)
    assert r_client.reason is ErrorCode.RATE_LIMITED_CLIENT
    assert r_api.reason is ErrorCode.RATE_LIMITED_API
    assert r_ca.reason is ErrorCode.RATE_LIMITED_CLIENT_API
    assert (r_client.level, r_api.level, r_ca.level) == (
        "client", "api", "client_api")


def test_refund_prevents_double_debit() -> None:
    """接口桶耗尽后，客户端桶被占用的令牌必须回补。"""
    async def scenario():
        cfg = make_config(client=(0.0, 5.0), api=(0.0, 2.0))
        lim = MultiLevelLimiter(cfg)
        await lim.check("c", "a")
        await lim.check("c", "a")
        rejected = await lim.check("c", "a")  # api 维度拒绝
        # 换一个接口（有独立大桶），客户端维度仍应有 3 枚
        ok1 = await lim.check("c", "b")
        ok2 = await lim.check("c", "b")
        ok3 = await lim.check("c", "b")
        return rejected, ok1, ok2, ok3

    rejected, ok1, ok2, ok3 = asyncio.run(scenario())
    logger.info("输入 api 桶耗尽拒绝=%s，随后其它接口放行=%s",
                rejected.allowed, [ok1.allowed, ok2.allowed, ok3.allowed])
    assert not rejected.allowed
    assert all(r.allowed for r in (ok1, ok2, ok3))


def test_state_restored_after_restart(tmp_path) -> None:
    path = os.path.join(tmp_path, "state.json")
    clock = {"t": 1000.0}

    async def scenario():
        cfg = make_config(client=(1.0, 5.0), api=(1.0, 5.0))
        store = QuotaStore(path)
        lim = MultiLevelLimiter(cfg, store, time_func=lambda: clock["t"])
        for _ in range(5):
            await lim.check("c", "a")
        await lim.flush()
        # 模拟进程重启，停机 2 秒（rate=1/s 补 2 枚）
        clock["t"] = 1002.0
        lim2 = MultiLevelLimiter(cfg, QuotaStore(path), time_func=lambda: clock["t"])
        return await asyncio.gather(*[lim2.check("c", "a") for _ in range(3)])

    results = asyncio.run(scenario())
    flags = [r.allowed for r in results]
    logger.info("输入 重启后3个请求（应恢复2枚），判定=%s", flags)
    assert flags == [True, True, False]
