"""多级限流器：客户端维度、接口维度、客户端×接口维度。

并发正确性约定：
- 单个 asyncio.Lock 串行化“补充 -> 判定 -> 扣减”全过程，杜绝超发；
- 三级顺序判定（client -> api -> client_api），后一级拒绝时把
  已扣减的令牌原样回补，杜绝重复扣减；
- 拒绝原因按维度区分（rate_limited.client / .api / .client_api）；
- 状态通过 QuotaStore 落盘，启动时按快照恢复并补算停机期间的令牌。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import QuotaConfig, QuotaRule
from app.errors import ErrorCode
from app.quota.bucket import TokenBucket
from app.quota.store import QuotaStore

logger = logging.getLogger("app.quota")


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    reason: ErrorCode | None
    retry_after: float
    level: str | None


_LEVELS = ("client", "api", "client_api")
_REASON_BY_LEVEL = {
    "client": ErrorCode.RATE_LIMITED_CLIENT,
    "api": ErrorCode.RATE_LIMITED_API,
    "client_api": ErrorCode.RATE_LIMITED_CLIENT_API,
}


class MultiLevelLimiter:
    def __init__(
        self,
        config: QuotaConfig,
        store: QuotaStore | None = None,
        *,
        time_func=None,
    ) -> None:
        self.config = config
        self.store = store
        self._time = time_func or time.time
        self._lock = asyncio.Lock()
        self._buckets: dict[tuple, TokenBucket] = {}
        self._last_flush = 0.0
        self.restore()

    # ----- 内部工具（须持锁调用） -----

    def _rule_for(self, level: str, client_id: str, api: str) -> QuotaRule | None:
        if level == "client":
            return self.config.client_rules.get(client_id, self.config.default_client)
        if level == "api":
            return self.config.api_rules.get(api, self.config.default_api)
        return self.config.ca_rules.get((client_id, api))

    def _bucket_key(self, level: str, client_id: str, api: str) -> tuple:
        if level == "client":
            return ("client", client_id)
        if level == "api":
            return ("api", api)
        return ("client_api", client_id, api)

    def _get_bucket(self, level: str, client_id: str, api: str, now: float) -> TokenBucket | None:
        rule = self._rule_for(level, client_id, api)
        if rule is None:
            return None
        key = self._bucket_key(level, client_id, api)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(rule, now=now)
            self._buckets[key] = bucket
        return bucket

    # ----- 对外接口 -----

    async def check(self, client_id: str, api: str, *, tokens: float = 1.0) -> LimitResult:
        async with self._lock:
            now = self._time()
            acquired: list[tuple[TokenBucket, float]] = []
            for level in _LEVELS:
                bucket = self._get_bucket(level, client_id, api, now)
                if bucket is None:
                    continue
                decision = bucket.try_acquire(now, tokens)
                if not decision.allowed:
                    # 回补此前各级已扣减的令牌，保证配额不被重复/错误扣减
                    for used_bucket, used_tokens in acquired:
                        used_bucket.tokens = min(
                            used_bucket.rule.burst, used_bucket.tokens + used_tokens
                        )
                    reason = _REASON_BY_LEVEL[level]
                    logger.info(
                        "限流判定 client=%s api=%s level=%s 通过=%s 拒绝原因=%s "
                        "桶内可用=%.3f retry_after=%.3f 已回补=%d 级",
                        client_id, api, level, False, reason.value,
                        decision.available, decision.retry_after, len(acquired),
                    )
                    await self._maybe_flush_locked(now)
                    return LimitResult(False, reason, decision.retry_after, level)
                acquired.append((bucket, tokens))
            logger.info(
                "限流判定 client=%s api=%s 通过=%s 扣减级数=%d",
                client_id, api, True, len(acquired),
            )
            await self._maybe_flush_locked(now)
            return LimitResult(True, None, 0.0, None)

    def restore(self) -> None:
        """启动恢复：读取快照并按停机时长补算令牌。文件不存在则冷启动。"""
        self._buckets.clear()
        if self.store is None:
            return
        data = self.store.load()
        raw = data.get("buckets", {})
        now = self._time()
        for key_str, state in raw.items():
            rule = self._rule_for_key_str(key_str)
            if rule is None:
                continue
            self._buckets[self._parse_key(key_str)] = TokenBucket.restore(rule, state, now)
        if raw:
            logger.info("配额状态恢复完成 桶数=%d", len(self._buckets))

    def _rule_for_key_str(self, key_str: str) -> QuotaRule | None:
        parts = key_str.split(":", 1)[0], key_str.split(":", 1)[1] if ":" in key_str else ""
        level, rest = parts
        if level == "client":
            return self.config.client_rules.get(rest, self.config.default_client)
        if level == "api":
            return self.config.api_rules.get(rest, self.config.default_api)
        if level == "client_api":
            client_id, api = rest.split("|", 1)
            return self.config.ca_rules.get((client_id, api))
        return None

    @staticmethod
    def _parse_key(key_str: str) -> tuple:
        level, rest = key_str.split(":", 1)
        if level == "client_api":
            client_id, api = rest.split("|", 1)
            return ("client_api", client_id, api)
        return (level, rest)

    @staticmethod
    def _format_key(key: tuple) -> str:
        if key[0] == "client_api":
            return f"client_api:{key[1]}|{key[2]}"
        return f"{key[0]}:{key[1]}"

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _maybe_flush_locked(self, now: float) -> None:
        if self.store is None:
            return
        if self._last_flush == 0.0:
            self._last_flush = now
            await self._flush_locked()
            return
        if now - self._last_flush >= self.config.flush_interval:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if self.store is None:
            return
        snapshot = {
            "saved_at": time.time(),
            "buckets": {
                self._format_key(key): bucket.snapshot()
                for key, bucket in self._buckets.items()
            },
        }
        await asyncio.to_thread(self.store.save, snapshot)
        self._last_flush = self._time()
