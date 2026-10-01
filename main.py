"""FastAPI 配额网关入口。

启动时装配接入层（版本注册表、多级限流器、熔断器、上游客户端、降级策略），
并把 GatewayError 统一转成结构化错误响应。
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.deps import build_gateway
from app.api.routes import error_body, router
from app.errors import GatewayError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app() -> FastAPI:
    app = FastAPI(title="fastapi-quota-gateway", version="0.1.0")
    app.state.gateway = build_gateway()
    app.include_router(router)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.extra),
            headers=exc.headers,
        )

    @app.get("/")
    def read_root() -> dict:
        return {"service": "fastapi-quota-gateway", "docs": "/docs"}

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
