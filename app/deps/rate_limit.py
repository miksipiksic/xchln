"""Token-bucket rate limiting on POST /v1/reviews only.

GETs are never limited - polling a job and reading its stream must stay free,
which is what the contract requires.

Capacity equals the declared per-minute rate and refills at exactly that rate,
so a sustained 30/min never sheds while a burst past 30 does. Shedding is a 429
with Retry-After and the error envelope; it is never a 5xx.
"""

from __future__ import annotations

import time

from starlette.datastructures import Headers

from app.config import RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE
from app.deps.http import send_error
from app.errors import RATE_LIMITED


class TokenBucket:
    def __init__(self, capacity: int, per_minute: int) -> None:
        self.capacity = float(capacity)
        self.refill_per_second = per_minute / 60.0
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def take(self) -> tuple[bool, int]:
        now = time.monotonic()
        self.tokens = min(
            self.capacity, self.tokens + (now - self.updated) * self.refill_per_second
        )
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0
        deficit = 1.0 - self.tokens
        retry_after = max(1, int(deficit / self.refill_per_second + 0.999))
        return False, retry_after


class RateLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE)
            self._buckets[key] = bucket
        return bucket

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _is_submission(scope):
            await self.app(scope, receive, send)
            return

        # Keyed by caller so one token's burst cannot shed another's traffic.
        header = Headers(scope=scope).get("authorization", "")
        allowed, retry_after = self._bucket(header or "anonymous").take()
        if not allowed:
            await send_error(
                send,
                429,
                RATE_LIMITED,
                "rate limit exceeded; retry shortly",
                {"Retry-After": str(retry_after)},
            )
            return

        await self.app(scope, receive, send)


def _is_submission(scope) -> bool:
    return scope.get("method") == "POST" and scope.get("path", "").rstrip("/") == "/v1/reviews"
