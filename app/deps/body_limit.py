"""Reject request bodies over the declared 1 MiB ceiling with 413.

Content-Length is checked first so an oversized upload is refused before any of
it is read. That header is advisory, though - a chunked request has none - so
the body is also counted while it streams in and aborted the moment it crosses
the limit.

The body is buffered here (bounded by the limit, so at most 1 MiB) and replayed
to the application. That is what lets the route re-read the exact raw bytes for
the idempotency hash without a second read of the socket.
"""

from __future__ import annotations

from starlette.datastructures import Headers

from app.config import MAX_PAYLOAD_BYTES
from app.deps.http import is_protected, send_error
from app.errors import PAYLOAD_TOO_LARGE

_BODY_METHODS = ("POST", "PUT", "PATCH")


class BodyLimitMiddleware:
    def __init__(self, app, limit: int = MAX_PAYLOAD_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in _BODY_METHODS
            or not is_protected(scope)
        ):
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.limit:
                    await self._too_large(send)
                    return
            except ValueError:
                pass  # malformed header: fall through to counting bytes

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            total += len(chunks[-1])
            if total > self.limit:
                await self._too_large(send)
                return
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, send) -> None:
        await send_error(
            send,
            413,
            PAYLOAD_TOO_LARGE,
            f"request body exceeds {self.limit} bytes",
        )
