"""super_research endpoints.

Two routers share one implementation:

- ``router``       -> /api/v1/super/*   (canonical, documented)
- ``compat_router``-> /api/super/*      (byte-compatible with the legacy
  vite middleware so the existing SuperSite frontend keeps working when its
  /api/super calls are pointed at this backend)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.schemas.super import (
    SnapshotOut,
    SuperConfigUpdate,
    SuperSignalPage,
    SuperSignalOut,
    SuperStopRequest,
)
from app.services import super_research as svc

router = APIRouter(prefix="/super", tags=["super-research"])
compat_router = APIRouter(prefix="/super", include_in_schema=False)


# --- state / control (vite-compatible shapes) ------------------------------

def _state(request: Request) -> dict:
    want_all = request.query_params.get("all") in ("1", "true")
    return svc.build_state(want_all=want_all)


@router.get("/state", operation_id="getSuperState")
def get_state(request: Request) -> dict:
    """Full desk state: categories/tickers with live worker rows, A/B feeds,
    econ + gex blobs. Pass ?all=1 to merge the archive ledgers."""
    return _state(request)


@router.post("/on", operation_id="superOn")
def super_on() -> dict:
    """Start missing category supervisors (detached; they survive API
    restarts). Categories with zero enabled tickers are skipped."""
    try:
        return svc.start_supervisors()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"start failed: {exc}")


@router.post("/off", operation_id="superOff")
def super_off(payload: SuperStopRequest | None = None) -> dict:
    """Stop supervisors (all, or one category). The legacy stack has no stop
    endpoint — this replaces `python bots.py stop`."""
    return svc.stop_supervisors(payload.category if payload else None)


@router.post("/config", operation_id="setSuperConfig")
def set_config(payload: SuperConfigUpdate) -> dict:
    try:
        svc.write_enabled(payload.enabled)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"config write failed: {exc}")
    return {"ok": True}


@router.get("/config", operation_id="getSuperConfig")
def get_config() -> dict:
    try:
        return svc.read_config()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"config unreadable: {exc}")


# --- GEX / econ ------------------------------------------------------------

@router.get("/gex", operation_id="getGex")
def get_gex() -> dict:
    """Latest merged GEX view (gex_daily.json passthrough). This backend
    never calls the flashAlpha API — the 09:00 CST scheduled job owns the
    5-requests/day free-tier budget."""
    gex = svc.read_gex()
    if gex is None:
        raise HTTPException(status_code=404, detail="gex_daily.json not available yet")
    return gex


@router.get("/econ", operation_id="getEcon")
def get_econ() -> dict:
    econ = svc.read_econ()
    if econ is None:
        raise HTTPException(status_code=404, detail="econ_today.json not available yet")
    return econ


@router.post("/regenerate", operation_id="regenerateEngines")
def regenerate(
    categories: str | None = Query(
        default=None, description="comma-separated: etf,stock,crypto,india; omit for all"
    ),
    force: bool = Query(
        default=False,
        description="launch even if the last regenerate was under 24h ago",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Re-run every enabled engine for today (--once --backfill-today per
    category). Results land in the ledgers within minutes and the auto-sync
    loop stores them in SQLite; watch /super/sync/status.

    If the engines already regenerated in the past 24h, this returns
    ``recent: true`` without launching — pass ``force=true`` after the user
    confirms, or skip regeneration and turn on the live watcher instead."""
    cats = [c.strip() for c in categories.split(",") if c.strip()] if categories else None
    try:
        return svc.regenerate_engines(cats, db=db, force=force)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"regenerate failed: {exc}")


@router.post("/gex/reload", operation_id="reloadGex")
def reload_gex(db: Session = Depends(get_db)) -> dict:
    """Re-read gex_daily.json from disk and store today's snapshot in the
    DB. Deliberately does NOT call the flashAlpha API (free tier is 5
    requests/day, owned by the 09:00 CST scheduled job)."""
    with svc._SYNC_LOCK:
        result = svc.sync_snapshots(db)
    gex = svc.read_gex()
    if gex is None:
        raise HTTPException(status_code=404, detail="gex_daily.json not available yet")
    return {"reloaded": True, "snapshots": result, "gex": gex}


@router.post("/econ/refresh", operation_id="refreshEcon")
def refresh_econ() -> dict:
    """Regenerate econ_today.json (keyless: hardcoded calendar + one
    yfinance yields fetch, cached per day by the script itself)."""
    settings = get_settings()
    python = str(settings.super_python)
    if not Path(python).is_file():
        python = sys.executable
    try:
        proc = subprocess.run(
            [python, str(settings.super_dir / "econ_events.py")],
            cwd=str(settings.super_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"econ refresh failed: {exc}")
    econ = svc.read_econ()
    return {"ok": proc.returncode == 0, "econ": econ}


# --- SQLite history --------------------------------------------------------

@router.post("/sync", operation_id="syncSuperData")
def sync_all(
    include_archive: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    """Full forced ingest of every signal source (central ledgers, worker
    CSVs, gex/econ snapshots) into SQLite. The background auto-sync loop
    runs the same pass continuously; this endpoint forces one now."""
    return svc.sync_everything(db, include_archive=include_archive, force=True)


@router.get("/sync/status", operation_id="getSuperSyncStatus")
def sync_status() -> dict:
    """Health of the background ingest loop that guarantees every generated
    signal lands in the database."""
    return svc.AUTO_SYNC_STATUS


@router.get("/signals", operation_id="getSuperSignals", response_model=SuperSignalPage)
def get_signals(
    book: str | None = Query(default=None, pattern="^[abAB]$"),
    category: str | None = Query(default=None),
    ticker: str | None = Query(default=None),
    grade_min: int | None = Query(default=None, ge=2, le=5, description="min eng_hot grade"),
    days: int | None = Query(default=None, ge=1, le=3660),
    limit: int = Query(default=100, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> SuperSignalPage:
    total, items = svc.query_signals(
        db,
        book=book,
        category=category,
        ticker=ticker,
        grade_min=grade_min,
        days=days,
        limit=limit,
        offset=offset,
    )
    return SuperSignalPage(total=total, items=[SuperSignalOut.model_validate(s) for s in items])


@router.get("/snapshots", operation_id="getDailySnapshots", response_model=list[SnapshotOut])
def get_snapshots(
    kind: str = Query(default="gex", examples=["gex", "econ", "gex_raw_spy"]),
    limit: int = Query(default=30, ge=1, le=366),
    db: Session = Depends(get_db),
) -> list[SnapshotOut]:
    from sqlalchemy import select

    from app.models import DailySnapshot

    rows = db.scalars(
        select(DailySnapshot)
        .where(DailySnapshot.kind == kind)
        .order_by(DailySnapshot.snapshot_date.desc())
        .limit(limit)
    ).all()
    return [SnapshotOut.model_validate(r) for r in rows]


# --- legacy-compatible aliases (/api/super/*) ------------------------------

@compat_router.get("/state")
def compat_state(request: Request) -> dict:
    return _state(request)


@compat_router.post("/on")
def compat_on() -> dict:
    return svc.start_supervisors()


@compat_router.post("/config")
def compat_config(payload: SuperConfigUpdate) -> dict:
    svc.write_enabled(payload.enabled)
    return {"ok": True}
