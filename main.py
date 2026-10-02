"""FastAPI 入口：把接入层暴露为 HTTP 接口。"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import date

import uvicorn
from urllib.parse import quote

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from gateway import (
    ApiVersion,
    CircuitBreaker,
    CircuitBreakerConfig,
    CostPolicy,
    Gateway,
    GatewayError,
    MultiLevelRateLimiter,
    QuotaPlan,
    RateLimitPolicy,
    TieredQuotaManager,
    UpstreamError,
    UpstreamFailureKind,
    VersionRegistry,
    VersionRule,
)

logger = logging.getLogger("quota-gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATE_PATH = os.environ.get("GATEWAY_STATE_PATH", "gateway_state.json")
QUOTA_STATE_PATH = os.environ.get("GATEWAY_QUOTA_STATE_PATH", "gateway_quota.json")

# 套餐分档：未单独配置的客户落到 default；GATEWAY_CLIENT_PLANS 可覆盖，形如 "vip:pro,partner:standard"
PLANS = {
    "default": QuotaPlan("default", rate_per_sec=5.0, burst=10, quota_limit=1000),
    "standard": QuotaPlan("standard", rate_per_sec=20.0, burst=40, quota_limit=10000),
    "premium": QuotaPlan("premium", rate_per_sec=100.0, burst=200, quota_limit=100000),
}
CLIENT_PLANS = dict(
    pair.split(":", 1)
    for pair in os.environ.get("GATEWAY_CLIENT_PLANS", "").split(",")
    if ":" in pair
)

quota = TieredQuotaManager(
    PLANS, "default", client_plans=CLIENT_PLANS, state_path=QUOTA_STATE_PATH
)

limiter = MultiLevelRateLimiter(
    client_policy=RateLimitPolicy(rate_per_sec=5.0, burst=10),  # 兜底；实际按套餐解析
    endpoint_policies={
        "/api/data": RateLimitPolicy(rate_per_sec=20.0, burst=40),
    },
    client_policy_resolver=lambda cid: RateLimitPolicy(
        rate_per_sec=quota.plan_for(cid).rate_per_sec,
        burst=quota.plan_for(cid).burst,
    ),
    state_path=STATE_PATH,
)
versions = VersionRegistry(
    rules=[
        VersionRule(ApiVersion(1, 0), deprecated_since=date(2026, 1, 1),
                    sunset_on=date(2026, 12, 31)),
        VersionRule(ApiVersion(2, 0)),
    ]
)
breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30.0))
costs = CostPolicy(
    endpoint_weights={"/api/data": 1.0},
    default_weight=1.0,
    min_declared=1.0,
    max_declared=10.0,
)
gateway = Gateway(limiter, versions, breaker, quota=quota, costs=costs)

app = FastAPI(title="quota-gateway")


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    logger.info(
        "rejected method=%s path=%s client=%s reason=%s detail=%s",
        request.method, request.url.path,
        request.headers.get("x-client-id", "anonymous"), exc.reason, exc.detail,
    )
    headers = {}
    if hasattr(exc, "retry_after"):
        headers["Retry-After"] = str(exc.retry_after)
    quota_view = exc.detail.get("quota") if isinstance(exc.detail, dict) else None
    if quota_view:
        headers["X-Quota-Limit"] = str(quota_view["limit"])
        headers["X-Quota-Remaining"] = str(quota_view["remaining"])
        headers["X-Quota-Reset"] = str(quota_view["reset_at"])
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.reason, "message": exc.message, "detail": exc.detail},
        headers=headers,
    )


def call_upstream() -> dict:
    """模拟上游依赖：演示用，按环境变量注入故障。"""
    mode = os.environ.get("UPSTREAM_MODE", "ok")
    if mode == "timeout":
        time.sleep(0.05)
        raise UpstreamError(UpstreamFailureKind.TIMEOUT, "upstream timed out")
    if mode == "connection":
        raise UpstreamError(UpstreamFailureKind.CONNECTION, "connection refused")
    if mode == "flaky" and random.random() < 0.5:
        raise UpstreamError(UpstreamFailureKind.BUSINESS, "upstream 5xx")
    return {"data": "hello from upstream"}


@app.get("/api/data")
def api_data(
    x_client_id: str = Header(default="anonymous"),
    x_api_version: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
    x_request_cost: float | None = Header(default=None),
):
    resp = gateway.handle(
        client_id=x_client_id,
        endpoint="/api/data",
        requested_version=x_api_version,
        upstream=call_upstream,
        declared_cost=x_request_cost,
        request_id=x_request_id,
    )
    content = {"body": resp.body, "version": str(resp.version.version)}
    if resp.quota is not None:
        content["quota"] = {
            "plan": resp.quota.plan,
            "limit": resp.quota.limit,
            "remaining": resp.quota.remaining,
            "reset_at": resp.quota.reset_at,
            "cost": resp.quota.cost,
        }
    if resp.cost is not None:
        content["cost"] = {
            "applied": resp.cost.cost,
            "weight": resp.cost.weight,
            "declared": resp.cost.declared,
            "clamped": resp.cost.clamped,
        }
    logger.info(
        "served client=%s request_id=%s version=%s quota=%s",
        x_client_id, x_request_id, content["version"], content.get("quota"),
    )
    headers = {}
    if resp.version.warning:
        headers["Deprecation"] = "true"
        # HTTP 头仅支持 latin-1：弃用提示含中文，按 RFC 5987 做百分号编码
        headers["Warning"] = quote(resp.version.warning)
    if resp.quota is not None:
        headers["X-Quota-Limit"] = str(resp.quota.limit)
        headers["X-Quota-Remaining"] = str(resp.quota.remaining)
        headers["X-Quota-Reset"] = str(resp.quota.reset_at)
    return JSONResponse(content=content, headers=headers)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.on_event("shutdown")
def persist_state() -> None:
    limiter.snapshot()
    quota.snapshot()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
