"""Shared helper for emitting the error envelope from raw ASGI middleware."""

from __future__ import annotations

import json

from app.errors import envelope


async def send_error(
    send,
    status: int,
    code: str,
    message: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(envelope(code, message)).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for key, value in (extra_headers or {}).items():
        headers.append((key.encode("ascii"), str(value).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def is_protected(scope) -> bool:
    """Everything under /v1 is protected; /health and /spec are public."""
    path = scope.get("path", "")
    return path == "/v1" or path.startswith("/v1/")
