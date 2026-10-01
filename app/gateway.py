"""接入层编排：版本协商 -> 限流 -> 熔断 -> 上游调用/降级。

判定顺序与结论约定：
1. 版本协商：非法/淘汰版本直接拒绝（400/426），不消耗配额；
2. 多级限流：任一维度超限即 429，原因码区分维度；
3. 熔断：OPEN 期快速失败并降级（200 + degraded，原因 breaker.open），
   绝不把请求继续堆给上游；
4. 调上游：成功 -> record_success 并回源结果；失败 -> 分类为
   timeout/connection/business 并 record_failure；
   - 若熔断因此/已处于 OPEN：返回降级兜底（原因 breaker.open）；
   - 熔断仍 CLOSED：返回 502 与可区分的上游错误码。
所有关键判定均打印“请求输入 + 判定依据”日志。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.errors import ErrorCode, GatewayError
from app.resilience.breaker import BreakerState, CircuitBreaker
from app.resilience.classifier import classify, kind_to_code
from app.resilience.fallback import FallbackPolicy
from app.resilience.upstream import UpstreamClient
from app.quota.limiter import MultiLevelLimiter
from app.versioning.negotiator import VersionNegotiator

logger = logging.getLogger("app.gateway")


@dataclass
class GatewayResponse:
    status_code: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


class Gateway:
    def __init__(
        self,
        settings: Settings,
        limiter: MultiLevelLimiter,
        negotiator: VersionNegotiator,
        breakers: dict[str, CircuitBreaker],
        upstream: UpstreamClient,
        fallback: FallbackPolicy,
        *,
        time_func=None,
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self.negotiator = negotiator
        self.breakers = breakers
        self.upstream = upstream
        self.fallback = fallback
        self._time = time_func or time.time

    def _breaker_for(self, api: str) -> CircuitBreaker:
        breaker = self.breakers.get(api)
        if breaker is None:
            breaker = CircuitBreaker(api, self.settings.breaker)
            self.breakers[api] = breaker
        return breaker

    async def handle(self, api: str, client_id: str, version_raw: str | None,
                     payload: dict) -> GatewayResponse:
        logger.info("收到请求 api=%s client=%s version_header=%r payload=%s",
                    api, client_id, version_raw, payload)
        headers: dict[str, str] = {}

        # 1) 版本协商
        negotiation = self.negotiator.negotiate(api, version_raw)
        info = negotiation.resolved
        headers["X-Resolved-Version"] = str(info.version)
        if negotiation.warnings:
            # HTTP 头只能是 latin-1：使用 RFC 7234 标准英文告警，中文说明进响应体
            sunset = f'; sunset="{info.sunset_date}"' if info.sunset_date else ""
            successor = (f'; successor="{api}-v{info.successor}"'
                         if info.successor else "")
            headers["Warning"] = (
                f'299 gateway "Deprecated API version {info.version}"'
                f'{sunset}{successor}')
            headers["X-Api-Deprecated"] = "true"
            if info.sunset_date:
                headers["X-Api-Sunset"] = info.sunset_date
            if info.successor is not None:
                headers["X-Api-Successor-Version"] = str(info.successor)

        # 2) 多级限流
        limit = await self.limiter.check(client_id, api)
        if not limit.allowed:
            logger.info("拒绝请求 api=%s client=%s 依据=%s retry_after=%.3f",
                        api, client_id, limit.reason.value if limit.reason else None,
                        limit.retry_after)
            import math
            finite = math.isfinite(limit.retry_after)
            extra = {"level": limit.level}
            err_headers: dict[str, str] = {}
            if finite:
                rounded = max(0, round(limit.retry_after, 3))
                extra["retry_after"] = rounded
                err_headers["Retry-After"] = str(rounded)
            raise GatewayError(
                limit.reason or ErrorCode.INTERNAL_ERROR,
                f"请求超出 {limit.level} 维度配额",
                status_code=429,
                extra=extra,
                headers=err_headers,
            )
        headers["Retry-After"] = "0"

        # 3) 熔断判定
        now = self._time()
        breaker = self._breaker_for(api)
        verdict = await breaker.allow(now)
        if not verdict.allowed:
            fb = self.fallback.fallback(api, ErrorCode.BREAKER_OPEN)
            headers["X-Degraded"] = "true"
            headers["X-Degraded-Reason"] = ErrorCode.BREAKER_OPEN.value
            logger.info("降级返回 api=%s client=%s 依据=熔断开启状态=%s",
                        api, client_id, verdict.state.value)
            return GatewayResponse(200, fb.payload, headers)

        # 4) 调用上游
        try:
            result = await self.upstream.call(api, payload)
        except Exception as exc:  # noqa: BLE001 - 需统一分类后处理
            kind = classify(exc)
            code = kind_to_code(kind)
            await breaker.record_failure(self._time())
            logger.info("上游失败 api=%s client=%s 异常=%s 分类=%s 熔断状态=%s",
                        api, client_id, type(exc).__name__, kind.value,
                        breaker.state.value)
            if breaker.state is BreakerState.OPEN:
                fb = self.fallback.fallback(api, ErrorCode.BREAKER_OPEN)
                headers["X-Degraded"] = "true"
                headers["X-Degraded-Reason"] = ErrorCode.BREAKER_OPEN.value
                return GatewayResponse(200, fb.payload, headers)
            raise GatewayError(
                code,
                f"上游调用失败：{type(exc).__name__}: {exc}",
                status_code=502,
                extra={"upstream_error": kind.value},
            ) from exc

        await breaker.record_success(self._time())
        logger.info("回源成功 api=%s client=%s v%d", api, client_id,
                    negotiation.resolved.version)
        body: dict = {
            "api": api,
            "version": negotiation.resolved.version,
            "degraded": False,
            "data": result.payload,
        }
        if negotiation.warnings:
            body["deprecation_notice"] = negotiation.warnings[0]
        return GatewayResponse(200, body, headers)
