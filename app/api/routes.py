"""HTTP 路由：统一入口、健康检查与熔断状态观测。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.errors import ErrorCode

router = APIRouter()


def _gateway(request: Request):
    return request.app.state.gateway


@router.post("/invoke/{api}")
async def invoke(
    api: str,
    request: Request,
    x_client_id: str = Header(default="anonymous", alias="X-Client-Id"),
    x_api_version: str | None = Header(default=None, alias="X-API-Version"),
) -> JSONResponse:
    payload = await request.json()
    gw = _gateway(request)
    result = await gw.handle(api, x_client_id, x_api_version, payload)
    return JSONResponse(status_code=result.status_code, content=result.body,
                        headers=result.headers)


@router.get("/health")
async def health(request: Request) -> dict:
    gw = _gateway(request)
    now = gw._time()
    return {
        "status": "ok",
        "breakers": {
            name: {
                "state": breaker.current_state(now).value,
                "consecutive_failures": breaker.consecutive_failures,
                "consecutive_successes": breaker.consecutive_successes,
            }
            for name, breaker in gw.breakers.items()
        },
    }


def error_body(code: ErrorCode, message: str, extra: dict) -> dict:
    body = {"error": {"code": code.value, "message": message}}
    if extra:
        body["error"]["details"] = extra
    return body
