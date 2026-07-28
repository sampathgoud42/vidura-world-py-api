"""Vidura World API — FastAPI application factory.

Run locally:
    .venv\\Scripts\\uvicorn app.main:app --host 0.0.0.0 --port 8790

Interactive docs:
    Swagger UI  http://127.0.0.1:8790/docs
    ReDoc       http://127.0.0.1:8790/redoc
    OpenAPI     http://127.0.0.1:8790/openapi.json
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _run_super_sync() -> dict:
    """One forced-off (incremental) ingest pass in a worker thread."""
    from app.core.database import SessionLocal
    from app.services import super_research as svc

    db = SessionLocal()
    try:
        return svc.sync_everything(db, include_archive=True, force=False)
    finally:
        db.close()


async def _super_sync_loop(interval: int) -> None:
    """Continuously mirror every super_research signal into SQLite so the
    DB is the durable record even if nobody ever calls /super/sync."""
    from app.services import super_research as svc

    log = logging.getLogger("app.super_sync")
    svc.AUTO_SYNC_STATUS.update(enabled=True, interval_s=interval)
    await asyncio.sleep(5)  # let startup settle before the first pass
    while True:
        try:
            result = await asyncio.to_thread(_run_super_sync)
            svc.AUTO_SYNC_STATUS.update(
                runs=svc.AUTO_SYNC_STATUS["runs"] + 1,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                last_result=result,
            )
            new = (result.get("signals", {}).get("inserted", 0) or 0) + (
                result.get("workers", {}).get("inserted", 0) or 0
            )
            if new:
                log.info("auto-sync stored %s new signal(s)", new)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the loop alive on any single failure
            svc.AUTO_SYNC_STATUS.update(
                errors=svc.AUTO_SYNC_STATUS["errors"] + 1, last_error=str(exc)
            )
            log.warning("auto-sync pass failed: %s", exc)
        await asyncio.sleep(interval)


def _warn_on_duplicate_server(port: int) -> None:
    """A second uvicorn on the same port fails to bind but keeps running its
    background loops — that happened here and pegged the CPU. Shout early."""
    try:
        import psutil

        me = os.getpid()
        others = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["pid"] == me:
                    continue
                if not (proc.info.get("name") or "").lower().startswith("python"):
                    continue
                cmd = " ".join(proc.cmdline())
                if "uvicorn" in cmd and "app.main:app" in cmd:
                    others.append(proc.info["pid"])
            except psutil.Error:
                continue
        if others:
            logging.getLogger("app").warning(
                "Another Vidura API process is already running (pid %s). Two servers "
                "double every background loop — stop the old one.", others
            )
    except Exception:  # never block startup on a diagnostic
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    _warn_on_duplicate_server(8790)
    sync_task: asyncio.Task | None = None
    if settings.super_auto_sync and settings.super_dir.is_dir():
        sync_task = asyncio.create_task(_super_sync_loop(settings.super_sync_interval))
    yield
    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


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
        # GET /api/super/state stays open even when keyed: it is the legacy
        # read-only surface the unmodified SuperSite frontend polls without
        # headers. The mutating compat POSTs (/on, /config) remain gated.
        is_open_compat = (
            request.method == "GET" and request.url.path == "/api/super/state"
        )
        if expected and request.url.path.startswith("/api") and not is_open_compat:
            import hmac as _hmac

            provided = request.headers.get("X-API-Key", "")
            if not _hmac.compare_digest(provided.encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key"})
        return await call_next(request)

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    # Legacy-compatible /api/super/* aliases: exact vite-middleware shapes so
    # the existing SuperSite frontend can point straight at this backend.
    from app.api.v1.super import compat_router

    app.include_router(compat_router, prefix="/api")

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
