"""Application factory: middleware order, error envelope, lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
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
    )

    application.include_router(public.router)
    application.include_router(reviews.router)

    # add_middleware wraps the app, so the LAST registered runs FIRST. Auth is
    # therefore outermost: an unauthenticated oversized POST is a 401, and no
    # request body is read before the caller is known.
    application.add_middleware(BodyLimitMiddleware)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(AuthMiddleware)

    _install_error_handlers(application)
    return application


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
