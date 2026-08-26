"""Application factory: middleware order, error envelope, lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import SERVICE_VERSION
from app.deps.auth import AuthMiddleware
from app.deps.body_limit import BodyLimitMiddleware
from app.deps.rate_limit import RateLimitMiddleware
from app.errors import (
    INTERNAL,
    INVALID_JSON,
    NOT_FOUND,
    RATE_LIMITED,
    UNAUTHORIZED,
    ApiError,
    envelope,
)
from app.routes import public, reviews
from app.state import AppState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reviews")

# Any status that can escape the framework mapped onto the published vocabulary.
_STATUS_CODES = {
    401: UNAUTHORIZED,
    403: UNAUTHORIZED,
    404: NOT_FOUND,
    405: NOT_FOUND,
    429: RATE_LIMITED,
}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        state = AppState()
        application.state.service = state
        await state.runner.start()
        log.info("service %s ready", SERVICE_VERSION)
        try:
            yield
        finally:
            await state.runner.stop()

    application = FastAPI(
        title="AI Diff Review Service",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        description=(
            "Submit a unified diff, get structured review findings back.\n\n"
            "`/health` and `/spec` are public. Every `/v1` route needs a bearer "
            "token - click **Authorize** and paste it to try them here."
        ),
    )

    application.include_router(public.router)
    application.include_router(reviews.router)
    _document_auth(application)

    # add_middleware wraps the app, so the LAST registered runs FIRST. Auth is
    # therefore outermost: an unauthenticated oversized POST is a 401, and no
    # request body is read before the caller is known.
    application.add_middleware(BodyLimitMiddleware)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(AuthMiddleware)

    _install_error_handlers(application)
    return application


def _document_auth(application: FastAPI) -> None:
    """Advertise the bearer scheme in the OpenAPI document.

    Enforcement lives in AuthMiddleware, which sits outside the routing layer
    and so is invisible to FastAPI's schema generation. Without this, /docs
    renders no Authorize control and every /v1 call from the page is a 401 the
    reader cannot do anything about. This changes documentation only - the
    middleware remains the thing that actually checks the token.
    """

    def openapi() -> dict:
        if application.openapi_schema:
            return application.openapi_schema

        schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "The token issued with this deployment.",
            }
        }
        for path, item in schema["paths"].items():
            if not path.startswith("/v1"):
                continue
            for operation in item.values():
                if not isinstance(operation, dict):
                    continue
                operation["security"] = [{"bearerAuth": []}]
                _drop_stock_validation_error(operation)

        application.openapi_schema = schema
        return schema

    application.openapi = openapi  # type: ignore[method-assign]


def _drop_stock_validation_error(operation: dict) -> None:
    """Remove FastAPI's auto-generated 422 unless we documented it ourselves.

    The framework advertises `{"detail": [...]}` for any route with a validated
    parameter. This service never emits that shape - every non-2xx is the error
    envelope - so leaving it in would document a response that cannot occur.
    """
    response = operation.get("responses", {}).get("422")
    if not response:
        return
    schema_ref = (
        response.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref", "")
    )
    if "HTTPValidationError" in schema_ref:
        del operation["responses"]["422"]


def _install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.body(), headers=exc.headers or None
        )

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, INTERNAL)
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(code, message),
            headers=getattr(exc, "headers", None),
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content=envelope(INVALID_JSON, "request could not be parsed")
        )

    @application.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500, content=envelope(INTERNAL, "internal server error")
        )


app = create_app()
