"""Single source of truth for every declared limit.

``GET /spec`` serialises straight from these constants and the middleware,
chunker and worker pool consume the same ones, so the self-declaration can
never drift from actual behaviour.
"""

from __future__ import annotations

import os

SERVICE_VERSION = "1.0.0"
SPEC_VERSION = "1.0"

# --- declared limits --------------------------------------------------------
MAX_PAYLOAD_BYTES = 1_048_576       # 1 MiB request body ceiling -> 413
CHUNK_BYTES = 65_536                # 64 KiB max per chunk, split on file bounds
MAX_CONCURRENT_JOBS = 4             # worker pool size; a 5th job queues
RATE_LIMIT_PER_MINUTE = 30          # sustained POST /v1/reviews budget

# Token-bucket burst capacity. Equal to the per-minute rate: a sustained
# 30/min always passes, while a burst beyond 30 starts shedding with 429.
RATE_LIMIT_BURST = RATE_LIMIT_PER_MINUTE

DEFAULT_MAX_FINDINGS = 100
MAX_MAX_FINDINGS = 10_000

PROVIDERS = ("mock", "llm")
DEFAULT_PROVIDER = "mock"

# --- housekeeping (96h unattended window must not leak memory) --------------
MAX_JOBS_RETAINED = 5_000           # LRU cap on the job store
IDEMPOTENCY_TTL_SECONDS = 24 * 3600

# --- runtime configuration --------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bearer_token() -> str:
    """Read at call time so tests can swap it without reimporting."""
    return _env("API_BEARER_TOKEN")


def llm_settings() -> dict[str, object]:
    return {
        "api_key": _env("GROQ_API_KEY") or _env("LLM_API_KEY"),
        "base_url": _env("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        "model": _env("LLM_MODEL", "llama-3.3-70b-versatile"),
        "timeout": float(_env("LLM_TIMEOUT_SECONDS", "20") or 20),
        "max_chunks": int(_env("LLM_MAX_CHUNKS", "8") or 8),
    }


def spec_document() -> dict[str, object]:
    return {
        "specVersion": SPEC_VERSION,
        "providers": list(PROVIDERS),
        "limits": {
            "maxPayloadBytes": MAX_PAYLOAD_BYTES,
            "chunkBytes": CHUNK_BYTES,
            "maxConcurrentJobs": MAX_CONCURRENT_JOBS,
            "rateLimitPerMinute": RATE_LIMIT_PER_MINUTE,
        },
    }
