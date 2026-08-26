"""The error envelope: ``{"error": {"code", "message"}}`` on every non-2xx."""

from __future__ import annotations

from typing import Any

# The published code vocabulary. Nothing outside this set may ever leave the
# service.
UNAUTHORIZED = "unauthorized"
PAYLOAD_TOO_LARGE = "payload_too_large"
INVALID_JSON = "invalid_json"
INVALID_DIFF = "invalid_diff"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
NOT_FOUND = "not_found"
RATE_LIMITED = "rate_limited"
INTERNAL = "internal"

STATUS_FOR_CODE = {
    UNAUTHORIZED: 401,
    PAYLOAD_TOO_LARGE: 413,
    INVALID_JSON: 400,
    INVALID_DIFF: 422,
    IDEMPOTENCY_CONFLICT: 409,
    NOT_FOUND: 404,
    RATE_LIMITED: 429,
    INTERNAL: 500,
}


class ApiError(Exception):
    """Raised anywhere in the stack; rendered as the envelope by the handler."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or STATUS_FOR_CODE.get(code, 500)
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return envelope(self.code, self.message)


def envelope(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}
