"""熔断与降级子系统。"""

from app.resilience.breaker import BreakerState, CircuitBreaker, BreakerVerdict
from app.resilience.classifier import UpstreamErrorKind, classify
from app.resilience.fallback import FallbackPolicy
from app.resilience.upstream import UpstreamClient, UpstreamResult

__all__ = [
    "CircuitBreaker",
    "BreakerState",
    "BreakerVerdict",
    "UpstreamErrorKind",
    "classify",
    "FallbackPolicy",
    "UpstreamClient",
    "UpstreamResult",
]
