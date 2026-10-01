"""单个令牌桶：稳态速率 + 突发容量，支持时钟驱动补充。

约定：
- 桶内令牌数 tokens、上次补充时间 last_refill；
- 每次申请前先按经过时间线性补充，上限为桶容量 burst；
- try_acquire 自身不加锁，由调用方（限流器）持锁串行化，
  从而在并发下保证“先判定后扣减”的原子性，不超发、不重复扣减。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import QuotaRule


@dataclass(frozen=True)
class Decision:
    """一次令牌申请的判定结果。"""

    allowed: bool
    remaining: float
    retry_after: float
    available: float


class TokenBucket:
    """时钟由外部注入（now），便于确定性测试与按启动时刻恢复。"""

    def __init__(self, rule: QuotaRule, now: float = 0.0, tokens: float | None = None) -> None:
        self.rule = rule
        self.tokens = rule.burst if tokens is None else max(0.0, min(rule.burst, tokens))
        self.last_refill = now

    def _refill(self, now: float) -> None:
        """按经过时间补充令牌；时钟不允许回退。"""
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(self.rule.burst, self.tokens + elapsed * self.rule.rate)
            self.last_refill = now

    def try_acquire(self, now: float, tokens: float = 1.0) -> Decision:
        self._refill(now)
        available = self.tokens
        if available + 1e-9 >= tokens:
            self.tokens -= tokens
            return Decision(True, self.tokens, 0.0, available)
        retry_after = (tokens - available) / self.rule.rate if self.rule.rate > 0 else float("inf")
        return Decision(False, available, retry_after, available)

    def snapshot(self) -> dict:
        return {"tokens": round(self.tokens, 9), "last_refill": self.last_refill}

    @classmethod
    def restore(cls, rule: QuotaRule, state: dict, now: float = 0.0) -> "TokenBucket":
        """按持久化快照恢复：先恢复令牌数，再补算停机期间新增令牌。"""
        bucket = cls(rule, now=0.0, tokens=float(state.get("tokens", rule.burst)))
        bucket.last_refill = float(state.get("last_refill", 0.0))
        bucket._refill(now)
        return bucket
