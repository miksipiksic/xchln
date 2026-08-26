"""The two public routes: /health and /spec."""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.config import SERVICE_VERSION, spec_document

router = APIRouter()

_STARTED_AT = time.monotonic()


@router.get("/")
async def index() -> dict[str, object]:
    """A signpost, not part of the contract.

    Anyone checking the deployment by hand pastes the base URL into a browser
    first. A bare 404 there reads as "the service is down" even when it is
    perfectly healthy, so this points at the routes that do exist.
    """
    return {
        "service": "ai-diff-review",
        "version": SERVICE_VERSION,
        "status": "ok",
        "docs": "/docs",
        "routes": {
            "health": "GET /health",
            "spec": "GET /spec",
            "submit": "POST /v1/reviews",
            "result": "GET /v1/reviews/{jobId}",
            "stream": "GET /v1/reviews/{jobId}/stream",
        },
        "auth": "all /v1 routes require: Authorization: Bearer <token>",
    }


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": SERVICE_VERSION,
        "uptimeSeconds": round(time.monotonic() - _STARTED_AT, 3),
    }


@router.get("/spec")
async def spec() -> dict[str, object]:
    # Serialised straight from app.config, which the middleware, chunker and
    # worker pool also read - the declaration cannot drift from behaviour.
    return spec_document()
