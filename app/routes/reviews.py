"""The /v1/reviews routes: submit, poll, stream."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import (
    DEFAULT_MAX_FINDINGS,
    DEFAULT_PROVIDER,
    MAX_MAX_FINDINGS,
    PROVIDERS,
)
from app.diff.chunker import chunk_files
from app.diff.parser import DiffParseError, parse_diff
from app.errors import (
    IDEMPOTENCY_CONFLICT,
    INVALID_DIFF,
    INVALID_JSON,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    RATE_LIMITED,
    UNAUTHORIZED,
    ApiError,
    envelope,
)
from app.events.sse import SSE_HEADERS, parse_last_event_id, stream_job
from app.jobs.cache import body_hash, content_key
from app.jobs.queue import JobPayload
from app.models import Job

router = APIRouter(prefix="/v1/reviews")


def _options(payload: dict) -> tuple[str, int]:
    """Read options leniently.

    The published error vocabulary has no code for "bad option value", so an
    unknown provider or a non-integer maxFindings falls back to its default
    rather than inventing an error code. Unknown fields are simply never read.
    """
    raw = payload.get("options")
    options = raw if isinstance(raw, dict) else {}

    provider = options.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER

    max_findings = options.get("maxFindings", DEFAULT_MAX_FINDINGS)
    if isinstance(max_findings, bool) or not isinstance(max_findings, int):
        max_findings = DEFAULT_MAX_FINDINGS
    max_findings = max(0, min(max_findings, MAX_MAX_FINDINGS))

    return provider, max_findings


_EXAMPLE_DIFF = (
    "--- a/pay.js\n"
    "+++ b/pay.js\n"
    "@@ -1,1 +1,4 @@\n"
    " function pay(id) {\n"
    '+  const sql = "SELECT * FROM orders WHERE id = " + id;\n'
    "+  eval(id);\n"
    '+  console.log(sql); // TODO remove\n'
)

# The body is read and validated by hand (see the docstring below), so FastAPI
# cannot infer a schema for it and /docs would otherwise render no editor at
# all - and send an empty body. This describes the shape for documentation
# only; nothing here validates the request.
_SUBMIT_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["diff"],
                "properties": {
                    "diff": {
                        "type": "string",
                        "description": "A unified diff. Added lines are what get reviewed.",
                    },
                    "options": {
                        "type": "object",
                        "properties": {
                            "provider": {
                                "type": "string",
                                "enum": list(PROVIDERS),
                                "default": DEFAULT_PROVIDER,
                            },
                            "maxFindings": {
                                "type": "integer",
                                "default": DEFAULT_MAX_FINDINGS,
                                "minimum": 0,
                            },
                        },
                    },
                },
            },
            "example": {
                "diff": _EXAMPLE_DIFF,
                "options": {"provider": "mock", "maxFindings": 100},
            },
        }
    },
}

_IDEMPOTENCY_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": False,
    "schema": {"type": "string"},
    "description": (
        "Optional. The same key with a byte-identical body returns the same "
        "jobId; the same key with a different body is a 409."
    ),
}


def _error_doc(code: str, description: str, message: str) -> dict:
    """Document a real error response.

    FastAPI's stock 422 describes `{"detail": [...]}`, a shape this service
    never emits - every non-2xx is the envelope. Declaring these explicitly
    replaces the framework's guess with the truth.
    """
    return {
        "description": description,
        "content": {"application/json": {"example": envelope(code, message)}},
    }


_SUBMIT_RESPONSES: dict = {
    202: {
        "description": "Accepted. Processing is asynchronous - poll the job or open its stream.",
        "content": {
            "application/json": {
                "example": {"jobId": "9da03113d8af4b1f8367a1be058276ec", "status": "queued"}
            }
        },
    },
    400: _error_doc(INVALID_JSON, "Body is not valid JSON", "request body is not valid JSON"),
    401: _error_doc(UNAUTHORIZED, "Missing or wrong bearer token", "missing or invalid bearer token"),
    409: _error_doc(
        IDEMPOTENCY_CONFLICT,
        "Idempotency-Key reused with a different body",
        "Idempotency-Key was already used with a different request body",
    ),
    413: _error_doc(PAYLOAD_TOO_LARGE, "Body over 1 MiB", "request body exceeds 1048576 bytes"),
    422: _error_doc(
        INVALID_DIFF,
        "diff missing, empty, or not a parseable unified diff",
        "'diff' is required and must be a non-empty string",
    ),
    429: _error_doc(RATE_LIMITED, "Rate limit exceeded", "rate limit exceeded; retry shortly"),
}


@router.post(
    "",
    status_code=202,
    summary="Submit a diff for review",
    responses=_SUBMIT_RESPONSES,
    openapi_extra={"requestBody": _SUBMIT_BODY, "parameters": [_IDEMPOTENCY_HEADER]},
)
@router.post("/", include_in_schema=False)
async def submit_review(request: Request) -> JSONResponse:
    state = request.app.state.service
    raw = await request.body()

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(INVALID_JSON, f"request body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ApiError(INVALID_DIFF, "body must be a JSON object containing 'diff'")

    diff = payload.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        raise ApiError(INVALID_DIFF, "'diff' is required and must be a non-empty string")

    provider, max_findings = _options(payload)

    try:
        files = parse_diff(diff)
    except DiffParseError as exc:
        raise ApiError(INVALID_DIFF, f"diff is not a parseable unified diff: {exc}") from exc

    chunks = chunk_files(files)
    key = content_key(diff, provider, max_findings)
    input_bytes = len(diff.encode("utf-8"))

    # --- idempotency: same key + identical body -> the same job ------------
    idem_key = request.headers.get("idempotency-key")
    digest = body_hash(raw)
    if idem_key:
        existing = state.idempotency.lookup(idem_key)
        if existing is not None:
            stored_digest, stored_job_id = existing
            if stored_digest != digest:
                raise ApiError(
                    IDEMPOTENCY_CONFLICT,
                    "Idempotency-Key was already used with a different request body",
                )
            return _accepted(stored_job_id)

    # --- caching: identical content -> mirror the earlier result ----------
    source: Job | None = None
    cached_id = state.cache.get(key)
    if cached_id:
        source = state.store.get(cached_id)

    job = state.store.create(
        provider=provider,
        max_findings=max_findings,
        content_key=key,
        input_bytes=input_bytes,
        chunks=len(chunks),
        cache_hit=source is not None,
    )
    job_payload = JobPayload(chunks=chunks)

    if source is not None:
        await state.runner.submit_mirror(job, source, job_payload)
    else:
        # Registered before the work starts, so concurrent identical
        # submissions mirror rather than duplicating the scan. Dropped again if
        # the job fails, so a failure is never served from cache.
        state.cache.put(key, job.id)
        await state.runner.submit(job, job_payload)

    if idem_key:
        state.idempotency.put(idem_key, digest, job.id)

    return _accepted(job.id)


@router.get(
    "/{job_id}",
    summary="Poll a review job",
    responses={
        200: {
            "description": "The job. `findings` is present once status is `done`.",
            "content": {
                "application/json": {
                    "example": {
                        "jobId": "9da03113d8af4b1f8367a1be058276ec",
                        "status": "done",
                        "findings": [
                            {
                                "id": "MOCK-001:pay.js:3",
                                "ruleId": "MOCK-001",
                                "path": "pay.js",
                                "line": 3,
                                "severity": "critical",
                                "category": "security",
                                "title": "eval usage",
                                "evidence": "  eval(id);",
                            }
                        ],
                        "usage": {"inputBytes": 167, "chunks": 1, "cacheHit": False},
                    }
                }
            },
        },
        401: _error_doc(UNAUTHORIZED, "Missing or wrong bearer token", "missing or invalid bearer token"),
        404: _error_doc(NOT_FOUND, "No such job", "no review job with id 'abc'"),
    },
)
async def get_review(job_id: str, request: Request) -> JSONResponse:
    job = request.app.state.service.store.get(job_id)
    if job is None:
        raise ApiError(NOT_FOUND, f"no review job with id {job_id!r}")
    return JSONResponse(status_code=200, content=job.to_dict())


@router.get(
    "/{job_id}/stream",
    summary="Stream a review job (Server-Sent Events)",
    description=(
        "Emits `status` on every transition, one `finding` per finding in final "
        "order, then `done` and closes. Connecting to a finished job replays the "
        "whole sequence identically. `Last-Event-ID` resumes without repeating.\n\n"
        "Swagger buffers the stream and shows it once complete; to watch events "
        "arrive live use `curl -N`."
    ),
    responses={
        200: {
            "description": "An event stream.",
            "content": {
                "text/event-stream": {
                    "example": (
                        'id: 1\nevent: status\ndata: {"jobId":"9da0...","status":"queued"}\n\n'
                        'id: 2\nevent: status\ndata: {"jobId":"9da0...","status":"running"}\n\n'
                        'id: 3\nevent: finding\ndata: {"id":"MOCK-001:pay.js:3",...}\n\n'
                        'id: 4\nevent: done\ndata: {"total":1,"usage":{...}}\n\n'
                    )
                }
            },
        },
        401: _error_doc(UNAUTHORIZED, "Missing or wrong bearer token", "missing or invalid bearer token"),
        404: _error_doc(NOT_FOUND, "No such job", "no review job with id 'abc'"),
    },
)
async def stream_review(job_id: str, request: Request) -> StreamingResponse:
    job = request.app.state.service.store.get(job_id)
    if job is None:
        raise ApiError(NOT_FOUND, f"no review job with id {job_id!r}")

    last_event_id = parse_last_event_id(request.headers.get("last-event-id"))
    return StreamingResponse(
        stream_job(job, last_event_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _accepted(job_id: str) -> JSONResponse:
    # The contract documents this exact shape for 202, including on an
    # idempotent replay of a job that has already progressed.
    return JSONResponse(status_code=202, content={"jobId": job_id, "status": "queued"})
