"""The two public routes: /health and /spec."""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.config import SERVICE_VERSION, spec_document

router = APIRouter()

_STARTED_AT = time.monotonic()


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
