"""Content cache and idempotency index.

Two different mechanisms that are easy to conflate:

* **Idempotency** is keyed by the client's ``Idempotency-Key`` header plus a
  hash of the raw request body. Same key + identical body -> the *same jobId*.
  Same key + different body -> 409.
* **Caching** is keyed by the *content* of the review request - the diff plus
  the effective options - with no header involved. A repeat submission must not
  redo the work; it gets a fresh jobId whose result mirrors the original and
  reports ``cacheHit: true``.

They are kept apart because a cache hit must NOT reuse the original jobId: that
job already reported ``cacheHit: false``, and flipping it retroactively would
make its own earlier response a lie.
"""

from __future__ import annotations

import hashlib
import json
import time

from app.config import IDEMPOTENCY_TTL_SECONDS


def content_key(diff: str, provider: str, max_findings: int) -> str:
    """Hash the *effective* request, with option defaults already applied.

    Canonicalising this way means an omitted ``options`` block and an explicit
    ``{"provider": "mock", "maxFindings": 100}`` collide correctly, which is a
    superset of the byte-identical requirement in the contract.
    """
    payload = json.dumps(
        {"diff": diff, "provider": provider, "maxFindings": max_findings},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def body_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


class ContentCache:
    """content_key -> jobId of the job that first computed it."""

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._entries.get(key)

    def put(self, key: str, job_id: str) -> None:
        self._entries[key] = job_id

    def discard(self, key: str, job_id: str) -> None:
        """Drop a failed job's entry so the next caller retries the work."""
        if self._entries.get(key) == job_id:
            del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()


class IdempotencyIndex:
    """Idempotency-Key -> (body hash, jobId), with a TTL."""

    def __init__(self, ttl: float = IDEMPOTENCY_TTL_SECONDS) -> None:
        self._entries: dict[str, tuple[str, str, float]] = {}
        self._ttl = ttl

    def lookup(self, key: str) -> tuple[str, str] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        digest, job_id, stored_at = entry
        if time.time() - stored_at > self._ttl:
            del self._entries[key]
            return None
        return digest, job_id

    def put(self, key: str, digest: str, job_id: str) -> None:
        self._entries[key] = (digest, job_id, time.time())

    def clear(self) -> None:
        self._entries.clear()
