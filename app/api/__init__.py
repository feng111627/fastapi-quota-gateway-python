"""FastAPI 路由与应用装配。"""

from app.api.deps import build_gateway
from app.api.routes import router

__all__ = ["router", "build_gateway"]
