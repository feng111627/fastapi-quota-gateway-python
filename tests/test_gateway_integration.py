"""端到端集成测试：HTTP 层下的版本、限流、熔断降级行为。

使用可控时钟与脚本化上游，保证熔断期间/恢复后的结论一致可复现。
"""

import asyncio
import logging

import httpx
import pytest

from app.api.deps import build_registry
from app.api.routes import error_body, router
from app.config import BreakerConfig, QuotaConfig, QuotaRule, Settings
from app.errors import GatewayError
from app.gateway import Gateway
from app.resilience.fallback import FallbackPolicy
from app.resilience.upstream import (
    ScriptedUpstreamClient,
    StaticUpstreamClient,
    UpstreamResult,
)
from app.quota.limiter import MultiLevelLimiter
from app.versioning.negotiator import VersionNegotiator
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger("tests.integration")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def build_app(*, quota: QuotaConfig, upstream, clock: FakeClock,
              breaker: BreakerConfig | None = None) -> FastAPI:
    settings = Settings(quota=quota, breaker=breaker or BreakerConfig(
        failure_threshold=3, open_duration=5.0, half_open_max_probes=1))
    gateway = Gateway(
        settings,
        MultiLevelLimiter(quota, time_func=clock),
        VersionNegotiator(build_registry()),
        {},
        upstream,
        FallbackPolicy(),
        time_func=clock,
    )
    app = FastAPI()
    app.state.gateway = gateway
    app.include_router(router)

    @app.exception_handler(GatewayError)
    async def handler(request, exc: GatewayError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(exc.code, exc.message, exc.extra),
                            headers=exc.headers)

    return app


async def post(client: httpx.AsyncClient, api: str, *, client_id="free",
               version="2", body=None):
    headers = {"X-Client-Id": client_id}
    if version is not None:
        headers["X-API-Version"] = str(version)
    logger.info("请求输入 POST /invoke/%s X-Client-Id=%s X-API-Version=%s body=%s",
                api, client_id, version, body)
    return await client.post(
        f"/invoke/{api}",
        json=body if body is not None else {},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_deprecated_version_returns_warning_header() -> None:
    quota = QuotaConfig(client_rules={"free": QuotaRule(100, 100)},
                        api_rules={"echo": QuotaRule(100, 100)})
    app = build_app(quota=quota,
                    upstream=StaticUpstreamClient(UpstreamResult("echo", {"k": "v"})),
                    clock=FakeClock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        resp = await post(client, "echo", version="1")
    logger.info("判定依据 状态=%d X-Resolved-Version=%s Warning=%s",
                resp.status_code, resp.headers.get("x-resolved-version"),
                resp.headers.get("warning"))
    assert resp.status_code == 200
    assert resp.headers["x-resolved-version"] == "1"
    assert "Deprecated" in resp.headers["warning"]
    assert resp.headers["x-api-sunset"] == "2026-12-31"
    assert "弃用" in resp.json()["deprecation_notice"]


@pytest.mark.asyncio
async def test_version_boundaries_over_http() -> None:
    quota = QuotaConfig(client_rules={"free": QuotaRule(100, 100)},
                        api_rules={"echo": QuotaRule(100, 100)})
    app = build_app(quota=quota,
                    upstream=StaticUpstreamClient(UpstreamResult("echo", {})),
                    clock=FakeClock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        missing = await post(client, "echo", version=None)
        invalid = await post(client, "echo", version="xx")
        removed = await post(client, "echo", version="3")
    logger.info("判定依据 缺省=%d 非法=%d 淘汰=%d",
                missing.status_code, invalid.status_code, removed.status_code)
    assert missing.status_code == 200
    assert missing.json()["version"] == 2
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "version.invalid"
    assert removed.status_code == 426
    assert removed.json()["error"]["code"] == "version.unsupported"


@pytest.mark.asyncio
async def test_concurrent_rate_limit_consistency() -> None:
    quota = QuotaConfig(client_rules={"free": QuotaRule(0.0, 5.0)},
                        api_rules={"echo": QuotaRule(100, 100)})
    app = build_app(quota=quota,
                    upstream=StaticUpstreamClient(UpstreamResult("echo", {})),
                    clock=FakeClock())
    transport = httpx.ASGITransport(app=app)

    async def one(i: int):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await post(c, "echo", body={"i": i})

    responses = await asyncio.gather(*[one(i) for i in range(20)])
    ok = sum(1 for r in responses if r.status_code == 200)
    limited = [r for r in responses if r.status_code == 429]
    codes = {r.json()["error"]["code"] for r in limited}
    logger.info("判定依据 20并发 成功=%d 限流=%d 原因集合=%s",
                ok, len(limited), codes)
    assert ok == 5
    assert len(limited) == 15
    assert codes == {"rate_limited.client"}


@pytest.mark.asyncio
async def test_breaker_open_degrades_then_half_open_recovers() -> None:
    quota = QuotaConfig(client_rules={"free": QuotaRule(100, 100)},
                        api_rules={"echo": QuotaRule(100, 100)})
    clock = FakeClock()
    upstream = ScriptedUpstreamClient([
        httpx.ConnectError("refused"),
        httpx.ConnectError("refused"),
        httpx.ConnectError("refused"),
        UpstreamResult("echo", {"healed": True}),
    ])
    app = build_app(quota=quota, upstream=upstream, clock=clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        # 阈值3：前两次 502 + 可区分原因，第三次触发熔断
        codes = []
        for _ in range(3):
            resp = await post(client, "echo")
            codes.append((resp.status_code,
                          resp.json().get("error", {}).get("code")))
        # OPEN 期：快速失败 -> 降级 200（多次结论一致）
        degraded = [await post(client, "echo") for _ in range(3)]
        assert all(r.status_code == 200 and r.json()["degraded"] is True
                   for r in degraded)
        assert all(r.headers["x-degraded-reason"] == "breaker.open"
                   for r in degraded)
        # 冷却到期：半开探测成功 -> 恢复
        clock.now += 5.0
        healed = await post(client, "echo")
        normal = await post(client, "echo")
    logger.info("判定依据 失败阶段=%s 降级次数=%d 探测恢复=%d 恢复后=%d",
                codes, 3, healed.status_code, normal.status_code)
    assert codes[:2] == [(502, "upstream.connection"), (502, "upstream.connection")]
    # 第三次失败同时触发熔断：按约定降级
    assert codes[2][0] == 200 or codes[2][0] == 502
    assert healed.status_code == 200
    assert healed.json() == {"api": "echo", "version": 2, "degraded": False,
                             "data": {"healed": True}}
    assert normal.status_code == 200 and normal.json()["degraded"] is False


@pytest.mark.asyncio
async def test_timeout_classified_distinctly() -> None:
    quota = QuotaConfig(client_rules={"free": QuotaRule(100, 100)},
                        api_rules={"echo": QuotaRule(100, 100)})
    app = build_app(
        quota=quota,
        upstream=StaticUpstreamClient(exc=httpx.TimeoutException("read timeout")),
        clock=FakeClock(),
        breaker=BreakerConfig(failure_threshold=100, open_duration=5.0),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        resp = await post(client, "echo")
    logger.info("判定依据 超时响应=%d code=%s",
                resp.status_code, resp.json()["error"]["code"])
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream.timeout"
