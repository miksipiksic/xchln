"""Bearer authentication for every /v1 route, every method.

Implemented as raw ASGI middleware rather than a route dependency so that it is
the *outermost* layer: an unauthenticated 2 MiB POST gets 401, not 413, and an
anonymous caller learns nothing about the service's internals. It also
guarantees the SSE route cannot be reached without a token, which a per-route
dependency is easy to forget on a streaming endpoint.
"""

from __future__ import annotations

import hmac

from starlette.datastructures import Headers

from app.config import bearer_token
from app.deps.http import is_protected, send_error
from app.errors import UNAUTHORIZED


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not is_protected(scope):
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization", "")
        scheme, _, credentials = header.partition(" ")
        expected = bearer_token()

        # Fail closed: an unconfigured server rejects everything rather than
        # silently serving an open API.
        authorised = bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(
            credentials.strip(), expected
        )

        if not authorised:
            await send_error(
                send,
                401,
                UNAUTHORIZED,
                "missing or invalid bearer token",
                {"WWW-Authenticate": "Bearer"},
            )
            return

        await self.app(scope, receive, send)
