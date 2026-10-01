"""FastAPI 入口：把接入层暴露为 HTTP 接口。"""

from __future__ import annotations

import logging
import os
import random
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway import (
    ApiVersion,
    CircuitBreaker,
    CircuitBreakerConfig,
    Gateway,
    GatewayError,
    MultiLevelRateLimiter,
    RateLimitPolicy,
    UpstreamError,
    UpstreamFailureKind,
    VersionRegistry,
    VersionRule,
)

logger = logging.getLogger("quota-gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATE_PATH = os.environ.get("GATEWAY_STATE_PATH", "gateway_state.json")

limiter = MultiLevelRateLimiter(
    client_policy=RateLimitPolicy(rate_per_sec=5.0, burst=10),
    endpoint_policies={
        "/api/data": RateLimitPolicy(rate_per_sec=20.0, burst=40),
    },
    state_path=STATE_PATH,
)
versions = VersionRegistry(
    rules=[
        VersionRule(ApiVersion(1, 0), deprecated_since=__import__("datetime").date(2026, 1, 1),
                    sunset_on=__import__("datetime").date(2026, 12, 31)),
        VersionRule(ApiVersion(2, 0)),
    ]
)
breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30.0))
gateway = Gateway(limiter, versions, breaker)

app = FastAPI(title="quota-gateway")


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    logger.info(
        "rejected method=%s path=%s reason=%s detail=%s",
        request.method, request.url.path, exc.reason, exc.detail,
    )
    headers = {}
    if hasattr(exc, "retry_after"):
        headers["Retry-After"] = str(exc.retry_after)
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
def api_data(x_client_id: str = "anonymous", x_api_version: str | None = None):
    resp = gateway.handle(
        client_id=x_client_id,
        endpoint="/api/data",
        requested_version=x_api_version,
        upstream=call_upstream,
    )
    content = {"body": resp.body, "version": str(resp.version.version)}
    headers = {}
    if resp.version.warning:
        headers["Deprecation"] = "true"
        headers["Warning"] = resp.version.warning
    return JSONResponse(content=content, headers=headers)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.on_event("shutdown")
def persist_state() -> None:
    limiter.snapshot()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
