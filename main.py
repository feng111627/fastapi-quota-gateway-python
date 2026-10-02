"""FastAPI 入口：把分档计费接入层暴露为 HTTP 接口。"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import random
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway import (
    AccountingGate,
    ApiVersion,
    BillingRejected,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpen,
    CostResolver,
    EndpointCostPolicy,
    GatewayError,
    Plan,
    PlanRegistry,
    QuotaExhausted,
    QuotaGateway,
    QuotaResetMode,
    RateLimitExceeded,
    UpstreamError,
    UpstreamFailureKind,
    VersionRegistry,
    VersionRejected,
    VersionRule,
)

logger = logging.getLogger("quota-gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STATE_PATH = os.environ.get("GATEWAY_STATE_PATH", "gateway_state.json")

# ---- 套餐档：默认档 + 单客户覆盖（运营可在此调整或通过 set_override 热更新） ----
plans = PlanRegistry(
    default=Plan(
        name="basic",
        rate_per_sec=5.0,
        burst=10,
        quota_limit=1000,
        quota_period_sec=86400.0,
        reset_mode=QuotaResetMode.DAILY,
    ),
    overrides={
        "pro": Plan(
            name="pro",
            rate_per_sec=20.0,
            burst=40,
            quota_limit=20000,
            quota_period_sec=86400.0,
            reset_mode=QuotaResetMode.DAILY,
        ),
    },
)

# 用 wall clock：DAILY 按 UTC 自然日重置需要纪元秒
def _wall_clock() -> float:
    return time.time()


accounting = AccountingGate(plans, state_path=STATE_PATH, clock=_wall_clock)

# ---- 接口权重与自声明代价区间 ----
costs = CostResolver(
    {
        "/api/data": EndpointCostPolicy(weight=1.0),
        "/api/heavy": EndpointCostPolicy(weight=5.0, min_declared=1.0, max_declared=20.0),
    }
)

versions = VersionRegistry(
    rules=[
        VersionRule(
            ApiVersion(1, 0),
            deprecated_since=_dt.date(2026, 1, 1),
            sunset_on=_dt.date(2026, 12, 31),
        ),
        VersionRule(ApiVersion(2, 0)),
    ]
)
breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30.0))
quota_gateway = QuotaGateway(accounting, versions, breaker, costs)

app = FastAPI(title="quota-gateway")


def _quota_payload(client_id: str) -> dict:
    return accounting.view(client_id).to_dict()


def _error_envelope(exc: GatewayError, client_id: str | None) -> dict:
    """统一错误信封：status/reason/message/quota/retry_after 字段稳定齐全。"""
    retry_after = getattr(exc, "retry_after", None)
    payload: dict = {
        "status": "rejected",
        "error": exc.reason,
        "reason": exc.reason,
        "message": exc.message,
        "detail": exc.detail,
        "retry_after": retry_after if retry_after is not None else 0.0,
        "quota": None,
    }
    # 额度视图对所有结局都尽力给出（包括 502/503），让客户能看到退还后的余额
    if client_id is not None:
        try:
            payload["quota"] = _quota_payload(client_id)
        except Exception:  # 视图是附加信息，绝不能改变错误结论
            payload["quota"] = None
    return payload


def _common_headers(exc: GatewayError) -> dict[str, str]:
    headers: dict[str, str] = {}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return headers


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    client_id = request.headers.get("x-client-id", "anonymous")
    logger.info(
        "rejected method=%s path=%s reason=%s retry_after=%s detail=%s",
        request.method, request.url.path, exc.reason,
        getattr(exc, "retry_after", None), exc.detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_envelope(exc, client_id),
        headers=_common_headers(exc),
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


def _handle_data(request: Request, endpoint: str):
    client_id = request.headers.get("x-client-id", "anonymous")
    request_id = request.headers.get("x-request-id") or None
    declared = request.headers.get("x-declared-cost")
    requested_version = request.headers.get("x-api-version")

    logger.info(
        "request client=%s endpoint=%s request_id=%s declared=%s version=%s",
        client_id, endpoint, request_id, declared, requested_version,
    )
    resp = quota_gateway.handle(
        client_id=client_id,
        endpoint=endpoint,
        requested_version=requested_version,
        upstream=call_upstream,
        declared_cost=declared,
        request_id=request_id,
    )
    content = resp.to_envelope()
    headers = {}
    if resp.version.warning:
        headers["Deprecation"] = "true"
        headers["Warning"] = resp.version.warning
    logger.info(
        "decision client=%s request_id=%s cost=%s replayed=%s quota=%s",
        client_id, request_id, resp.cost, resp.replayed, content["quota"],
    )
    return JSONResponse(content=content, headers=headers)


@app.get("/api/data")
def api_data(request: Request):
    return _handle_data(request, "/api/data")


@app.get("/api/heavy")
def api_heavy(request: Request):
    return _handle_data(request, "/api/heavy")


@app.get("/quota")
def quota_status(request: Request):
    """客户端自查额度：套餐、速率剩余、周期总量剩余与重置时间。"""
    client_id = request.headers.get("x-client-id", "anonymous")
    return {"status": "ok", "quota": _quota_payload(client_id)}


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.on_event("shutdown")
def persist_state() -> None:
    accounting.snapshot()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
