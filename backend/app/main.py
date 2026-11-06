"""
FastAPI Application Entry Point (Phase 5C).
Configures application lifecycle, CORS, centralized exception handlers,
and registers API routers under /api/v1.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app.api.v1 import api_v1_router
from backend.app.core.config import settings
from backend.app.core.database import async_session_factory, close_db, init_db
from backend.app.models.base import utc_now
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.services.run_service import RunService
from backend.app.services.spec_parser import (
    SpecParserError,
    SpecParsingError,
    SpecSecurityError,
    SpecValidationError,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages application startup and shutdown lifecycle.
    Initializes database tables, recovers any interrupted runs, and safely releases connections.
    """
    logger.info("Initializing database schema on startup...")
    await init_db()

    # Recover any runs left in 'running' state from previous process crashes
    try:
        async with async_session_factory() as session:
            run_service = RunService()
            recovered = await run_service.recover_interrupted_runs(session)
            if recovered > 0:
                await session.commit()
                logger.warning("Startup: recovered %d interrupted run(s).", recovered)
    except Exception as exc:
        logger.error("Startup run recovery encountered an error: %s", exc)

    logger.info("Application startup complete. Environment: %s", settings.ENVIRONMENT)
    yield

    logger.info("Closing database engine on shutdown...")
    await close_db()
    logger.info("Application shutdown complete.")


def create_application() -> FastAPI:
    """Factory function creating and configuring the FastAPI application instance."""
    app = FastAPI(
        title="Autonomous API Testing Agent Backend",
        version="1.0.0",
        description=(
            "FastAPI production backend orchestrating autonomous OpenAPI specification exploration, "
            "contract testing, failure investigation, and diagnostic reporting."
        ),
        lifespan=lifespan,
    )

    # -----------------------------------------------------------------------
    # CORS Configuration
    # -----------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.ENVIRONMENT == "development" else ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Centralized Exception Handlers
    # -----------------------------------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": err.get("loc", []),
                "msg": ReportDataSanitizer.sanitize_text(str(err.get("msg", ""))),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Request validation failed", "errors": errors},
        )

    @app.exception_handler(SpecParsingError)
    async def spec_parsing_exception_handler(request: Request, exc: SpecParsingError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": ReportDataSanitizer.sanitize_text(str(exc)), "error_type": "SpecParsingError"},
        )

    @app.exception_handler(SpecValidationError)
    async def spec_validation_exception_handler(request: Request, exc: SpecValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": ReportDataSanitizer.sanitize_text(str(exc)), "error_type": "SpecValidationError"},
        )

    @app.exception_handler(SpecSecurityError)
    async def spec_security_exception_handler(request: Request, exc: SpecSecurityError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": ReportDataSanitizer.sanitize_text(str(exc)), "error_type": "SpecSecurityError"},
        )

    @app.exception_handler(ValueError)
    async def value_error_exception_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": ReportDataSanitizer.sanitize_text(str(exc)), "error_type": "ValueError"},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": ReportDataSanitizer.sanitize_text(str(exc.detail))},
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled server exception: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An internal server error occurred.",
                "error_type": "InternalServerError",
            },
        )

    # -----------------------------------------------------------------------
    # Root Health Check & Router Mounting
    # -----------------------------------------------------------------------
    @app.get("/health", tags=["system"], summary="Service health status")
    async def health_check() -> dict:
        return {
            "status": "ok",
            "environment": settings.ENVIRONMENT,
            "timestamp": utc_now().isoformat(),
        }

    app.include_router(api_v1_router)

    return app


app = create_application()
