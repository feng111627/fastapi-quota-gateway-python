"""上游调用抽象：屏蔽 httpx，便于注入假上游做并发与熔断测试。

- UpstreamClient：抽象接口；
- HTTPUpstreamClient：基于 httpx 的真实实现，负责把网络/超时/非2xx
  统一抛出，交给 classifier 分类；
- StaticUpstreamClient / ScriptedUpstreamClient：测试替身。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.resilience.classifier import UpstreamStatusError


@dataclass(frozen=True)
class UpstreamResult:
    api: str
    payload: dict
    status_code: int = 200


class UpstreamClient:
    async def call(self, api: str, payload: dict) -> UpstreamResult:
        raise NotImplementedError


class HTTPUpstreamClient(UpstreamClient):
    """通过 HTTP POST 调用上游；base_url 可配置。"""

    def __init__(self, base_url: str, *, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def call(self, api: str, payload: dict) -> UpstreamResult:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/{api}", json=payload)
            if response.status_code // 100 != 2:
                raise UpstreamStatusError(response.status_code, response.text)
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text}
            return UpstreamResult(api=api, payload=body, status_code=response.status_code)


class StaticUpstreamClient(UpstreamClient):
    """始终返回固定结果或抛出固定异常的测试替身。"""

    def __init__(self, result: UpstreamResult | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def call(self, api: str, payload: dict) -> UpstreamResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class ScriptedUpstreamClient(UpstreamClient):
    """按调用次序依次返回结果/抛出异常的测试替身。"""

    def __init__(self, script: list[UpstreamResult | Exception]) -> None:
        self._script = list(script)
        self.calls = 0

    async def call(self, api: str, payload: dict) -> UpstreamResult:
        idx = self.calls
        self.calls += 1
        item = self._script[min(idx, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item
