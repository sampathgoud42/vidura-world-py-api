"""Vidura World API — FastAPI application factory.

Run locally:
    .venv\\Scripts\\uvicorn app.main:app --host 0.0.0.0 --port 8790

Interactive docs:
    Swagger UI  http://127.0.0.1:8790/docs
    ReDoc       http://127.0.0.1:8790/redoc
    OpenAPI     http://127.0.0.1:8790/openapi.json
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Multi-user backend for the 38trades trading bots (Kalshi BTC "
            "15m/60m, multi-sport), tennis prediction models, and the "
            "wellness app. SQLite persistence, per-user credential folders."
        ),
        lifespan=lifespan,
    )

    # Mobile/web clients are cross-origin; auth is header-based (no cookies),
    # so a permissive CORS policy is safe here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def api_key_guard(request: Request, call_next):
        # Shared-key gate (VIDURA_API_KEY). Docs and health stay open so the
        # Swagger UI can be browsed; every /api route requires the header.
        expected = get_settings().api_key
        if expected and request.url.path.startswith(settings.api_v1_prefix):
            import hmac as _hmac

            provided = request.headers.get("X-API-Key", "")
            if not _hmac.compare_digest(provided.encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key"})
        return await call_next(request)

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Frontends detect "backend present" by JSON content type — every
        # response, including errors, must be JSON.
        logging.getLogger("app").exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})

    @app.get("/", include_in_schema=False)
    def root():
        return {
            "app": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "api": settings.api_v1_prefix,
        }

    @app.get("/health", tags=["system"], operation_id="healthCheck")
    def health():
        return {"status": "ok", "database": str(settings.database_path), "paper_only": settings.paper_only}

    return app


app = create_app()
