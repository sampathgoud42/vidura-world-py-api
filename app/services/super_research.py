"""super_research service: state assembly, supervisor control, GEX/econ.

This ports the vite-middleware contract (web-app-main/vite.config.js) that
the SuperSite frontend already speaks, byte-compatible:

- every CSV-derived value stays a STRING (the frontend does exact string
  compares like ``hot === '1'`` and Number() coercion itself);
- econ/gex are BOM-tolerant verbatim file passthroughs, null when missing;
- worker rows: last 40 CSV lines, newest first, capped at 30, only for
  enabled tickers;
- central feeds: sorted DESC on ``logged_at_cst|bar_time_cst``, capped 150
  (2000 with all=1, which also merges the archive ledgers).

GEX safety: the flashAlpha key is FREE tier (5 requests/day; the 09:00
schtask uses 2). This module NEVER calls the flashAlpha API — it only reads
the JSON files the scheduled job produces.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DailySnapshot, SuperSignal

logger = logging.getLogger(__name__)

_CST = ZoneInfo("America/Chicago")

CATEGORY_SEQUENCE = ("etf", "stock", "crypto")

# Own spawned supervisors: category -> Popen. Machine-wide detection below
# also finds supervisors started by bots.py / schtask / vite.
_CHILDREN: dict[str, subprocess.Popen] = {}

# 8-second cache of the machine-wide supervisor scan (matches vite).
_SCAN_CACHE: dict = {"ts": 0.0, "running": {}}

# Sync endpoints run on the threadpool; serialize scan-then-spawn and the
# select-then-insert sync passes so concurrent requests cannot double-spawn
# supervisors or race the unique indexes.
_ON_LOCK = threading.Lock()
_SYNC_LOCK = threading.Lock()

# (mtime, size) per ingested file so the background loop skips unchanged
# files instead of re-upserting thousands of rows every minute.
_FILE_STATE: dict[str, tuple[float, int]] = {}

# Telemetry for the background loop, served by GET /super/sync/status.
AUTO_SYNC_STATUS: dict = {
    "enabled": False,
    "interval_s": None,
    "runs": 0,
    "errors": 0,
    "last_run_at": None,
    "last_error": None,
    "last_result": None,
}


def _file_changed(path: Path, *, force: bool) -> bool:
    """True if the file should be (re)ingested; records the new state."""
    try:
        stat = path.stat()
    except OSError:
        return False
    state = (stat.st_mtime, stat.st_size)
    if not force and _FILE_STATE.get(str(path)) == state:
        return False
    _FILE_STATE[str(path)] = state
    return True


# --- config file -----------------------------------------------------------

def config_path() -> Path:
    return get_settings().super_dir / "super_research.config"


def read_config() -> dict:
    raw = config_path().read_text(encoding="utf-8")
    return json.loads(raw.lstrip("﻿"))


def write_enabled(enabled: dict[str, bool]) -> dict:
    """Apply {tickerId: bool} across all categories, preserving every other
    key, and rewrite the config the same way the vite middleware does."""
    cfg = read_config()
    for cat in (cfg.get("categories") or {}).values():
        for t in cat.get("tickers") or []:
            if t.get("id") in enabled:
                t["enabled"] = bool(enabled[t["id"]])
    # Byte-compatible with vite's JSON.stringify(cfg, null, 2) + '\n':
    # literal UTF-8 (no \uXXXX escapes in the hand-maintained rules text)
    # and LF line endings even on Windows.
    config_path().write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _SCAN_CACHE["ts"] = 0.0  # force fresh liveness on next state
    return cfg


# --- file readers (string-preserving) --------------------------------------

def _read_csv_rows(path: Path) -> list[dict]:
    """DictReader keeps every value a string — required by the frontend."""
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            return [
                {k: (v if isinstance(v, str) else "") for k, v in row.items() if k}
                for row in csv.DictReader(fh)
            ]
    except OSError:
        return []


def read_worker_rows(path: Path, *, tail: int = 40, cap: int = 30) -> list[dict]:
    rows = _read_csv_rows(path)
    return list(reversed(rows[-tail:]))[:cap]


def read_central_feed(name: str, *, want_all: bool = False) -> list[dict]:
    super_dir = get_settings().super_dir
    rows = _read_csv_rows(super_dir / name)
    if want_all:
        rows += _read_csv_rows(super_dir / "archive" / name)
    rows.sort(
        key=lambda r: f"{r.get('logged_at_cst', '')}|{r.get('bar_time_cst', '')}",
        reverse=True,
    )
    return rows[: 2000 if want_all else 150]


def read_json_file(path: Path) -> dict | None:
    """BOM-tolerant JSON passthrough; None when missing/unparsable —
    the frontend treats null as 'not built yet'."""
    try:
        return json.loads(path.read_text(encoding="utf-8").lstrip("﻿"))
    except (OSError, json.JSONDecodeError):
        return None


def read_gex() -> dict | None:
    return read_json_file(get_settings().super_dir / "gex_daily.json")


def read_econ() -> dict | None:
    return read_json_file(get_settings().super_dir / "econ_today.json")


# --- supervisor liveness / control -----------------------------------------

def _scan_running(force: bool = False) -> dict[str, int]:
    """category -> pid for any python running super_signal_bot.py, however
    it was started (bots.py, schtask, vite, or us)."""
    now = time.monotonic()
    if not force and now - _SCAN_CACHE["ts"] < 8.0:
        return _SCAN_CACHE["running"]
    running: dict[str, int] = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if "super_signal_bot.py" not in joined:
                continue
            for i, arg in enumerate(cmdline):
                if arg == "--category" and i + 1 < len(cmdline):
                    running.setdefault(cmdline[i + 1], proc.info["pid"])
        except psutil.Error:
            continue
    _SCAN_CACHE.update(ts=now, running=running)
    return running


def _category_liveness(key: str, running: dict[str, int]) -> tuple[bool, int | None]:
    child = _CHILDREN.get(key)
    if child is not None and child.poll() is None:
        return True, running.get(key, child.pid)
    if key in running:
        return True, running[key]
    return False, None


def start_supervisors() -> dict:
    """POST /super/on: for each category with >=1 enabled ticker and no live
    supervisor, spawn one detached (bots.py-style, so it survives an API
    restart). Categories with zero enabled tickers are skipped; a category
    that fails to spawn is reported without aborting the rest (vite parity)."""
    settings = get_settings()
    cfg = read_config()
    started: list[str] = []
    already: list[str] = []
    failed: dict[str, str] = {}
    with _ON_LOCK:
        running = _scan_running(force=True)
        for key, cat in (cfg.get("categories") or {}).items():
            if not any(t.get("enabled") for t in cat.get("tickers") or []):
                continue
            live, _pid = _category_liveness(key, running)
            if live:
                already.append(key)
                continue
            try:
                python = str(settings.super_python)
                if not Path(python).is_file():
                    python = sys.executable
                bot = settings.super_dir / (cat.get("bot") or "super_signal_bot.py")
                if not bot.is_file():
                    raise OSError(f"bot script missing: {bot}")
                out = settings.super_dir / f"super_{key}.out"
                creationflags = 0
                if os.name == "nt":
                    DETACHED_PROCESS = 0x00000008
                    creationflags = DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                with open(out, "a", encoding="utf-8", errors="replace") as log_handle:
                    proc = subprocess.Popen(
                        [python, str(bot), "--category", key, "--poll", "60"],
                        cwd=str(settings.super_dir),
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        close_fds=True,
                        creationflags=creationflags,
                    )
                _CHILDREN[key] = proc
                started.append(key)
                logger.info("Started super supervisor %s (pid %s)", key, proc.pid)
            except OSError as exc:
                failed[key] = str(exc)
                logger.warning("Could not start super supervisor %s: %s", key, exc)
        _SCAN_CACHE["ts"] = 0.0
    result = {"started": started, "already": already, "sequence": started + already}
    if failed:
        result["failed"] = failed
    return result


def stop_supervisors(category: str | None = None) -> dict:
    """Stop supervisors by cmdline match, mirroring `python bots.py stop`:
    matches super_signal_bot.py AND stray _intraday_bot.py workers (full
    stop only), and kills each supervisor's child tree so in-flight workers
    do not linger past their parent."""
    victims: list[psutil.Process] = []
    seen: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            is_supervisor = "super_signal_bot.py" in joined
            is_worker = "_intraday_bot.py" in joined
            if not is_supervisor and not (is_worker and category is None):
                continue
            if is_supervisor and category is not None:
                cat = None
                for i, arg in enumerate(cmdline):
                    if arg == "--category" and i + 1 < len(cmdline):
                        cat = cmdline[i + 1]
                if cat != category:
                    continue
            if proc.pid in seen:
                continue
            seen.add(proc.pid)
            victims.append(proc)
            if is_supervisor:
                try:
                    for child in proc.children(recursive=True):
                        if child.pid not in seen:
                            seen.add(child.pid)
                            victims.append(child)
                except psutil.Error:
                    pass
        except psutil.Error:
            continue
    stopped: list[dict] = []
    for proc in victims:
        try:
            cmd = " ".join(proc.cmdline())[-120:]
            proc.terminate()
            stopped.append({"pid": proc.pid, "cmdline": cmd})
        except psutil.Error:
            continue
    if victims:
        _, alive = psutil.wait_procs(victims, timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
    if category is None:
        _CHILDREN.clear()
    else:
        _CHILDREN.pop(category, None)
    _SCAN_CACHE["ts"] = 0.0
    return {"stopped": stopped}


def _safe_proc(pid: int) -> psutil.Process | None:
    try:
        return psutil.Process(pid)
    except psutil.Error:
        return None


# --- state assembly ---------------------------------------------------------

def build_state(*, want_all: bool = False) -> dict:
    settings = get_settings()
    try:
        cfg = read_config()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"error": f"config unreadable: {exc}"}

    running = _scan_running()
    categories = []
    abook: list[dict] = []
    for key, cat in (cfg.get("categories") or {}).items():
        live, pid = _category_liveness(key, running)
        tickers = []
        for t in cat.get("tickers") or []:
            enabled = bool(t.get("enabled"))
            rows: list[dict] = []
            if enabled:
                rows = read_worker_rows(
                    settings.super_dir / (t.get("path") or "") / (t.get("csv") or "")
                )
                for row in rows:
                    if row.get("book") == "A":
                        abook.append({**row, "src": t.get("label"), "cat": key})
            tickers.append(
                {
                    "id": t.get("id"),
                    "label": t.get("label"),
                    "rules": t.get("rules"),
                    "img": t.get("img"),
                    "enabled": enabled,
                    "live": live and enabled,
                    "rows": rows,
                }
            )
        categories.append(
            {
                "key": key,
                "label": cat.get("label"),
                "session": cat.get("session") or "rth",
                "live": live,
                "pid": pid,
                "tickers": tickers,
            }
        )
    abook.sort(key=lambda r: r.get("bar_time_cst", ""), reverse=True)

    return {
        "abookOnTop": cfg.get("abookOnTop") is not False,
        "categories": categories,
        "abook": abook,
        "aFeed": read_central_feed("a_signals.csv", want_all=want_all),
        "bFeed": read_central_feed("b_signals.csv", want_all=want_all),
        "econ": read_econ(),
        "gex": read_gex(),
    }


# --- SQLite history: signals + daily snapshots ------------------------------

def _parse_cst(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            aware = datetime.strptime(value.strip(), fmt).replace(tzinfo=_CST)
            return aware.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _float_or_none(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def sync_signals(db: Session, *, include_archive: bool = True, force: bool = True) -> dict:
    """Ingest central A/B ledgers (+ archive) into the super_signals table.

    Ledger rows are updated in place when outcomes resolve, so this upserts
    on a stable external_id and refreshes outcome-bearing fields. With
    ``force=False`` files whose mtime+size are unchanged are skipped.
    """
    settings = get_settings()
    files = [
        (settings.super_dir / "a_signals.csv", "A"),
        (settings.super_dir / "b_signals.csv", "B"),
    ]
    if include_archive:
        files += [
            (settings.super_dir / "archive" / "a_signals.csv", "A"),
            (settings.super_dir / "archive" / "b_signals.csv", "B"),
        ]
    inserted = updated = 0
    report: dict[str, dict] = {}
    for path, book in files:
        if not path.is_file() or not _file_changed(path, force=force):
            continue
        rows = _read_csv_rows(path)
        file_ins = file_upd = 0
        # Pre-merge duplicate keys within the file (last row wins): the
        # ledgers can carry two rows for the same second+ticker+signal, and
        # the session is autoflush=False so a same-key SELECT would not see
        # a pending insert from an earlier row.
        merged: dict[str, dict] = {}
        for row in rows:
            ticker = (row.get("ticker") or "").strip()
            logged = (row.get("logged_at_cst") or "").strip()
            if not ticker or not logged:
                continue
            # bar_time_cst is part of the identity: two DISTINCT signals for
            # the same ticker+signal can flush in the same second when they
            # come from different bars (real occurrence in b_signals.csv).
            external_id = (
                f"central:{book}:{logged}:{ticker}:"
                f"{(row.get('bar_time_cst') or '').strip()}:"
                f"{(row.get('signal') or '')[:100]}"
            )
            merged[external_id] = row
        for external_id, row in merged.items():
            ticker = (row.get("ticker") or "").strip()
            logged = (row.get("logged_at_cst") or "").strip()
            mapped = {
                "book": book,
                "category": (row.get("category") or "").strip() or None,
                "ticker": ticker,
                "direction": (row.get("direction") or "").strip() or None,
                "grade": (row.get("eng_hot") or "").strip() or None,
                "combo": (row.get("signal") or "").strip() or None,
                "price": _float_or_none(row.get("signal_price")),
                "accuracy_pct": _float_or_none(row.get("acc_strict_pct")),
                "bar_time": (row.get("bar_time_cst") or "").strip() or None,
                "logged_at": _parse_cst(logged),
                "raw": {k: v for k, v in row.items() if k},
            }
            existing = db.scalar(
                select(SuperSignal).where(SuperSignal.external_id == external_id)
            )
            if existing is None:
                db.add(SuperSignal(external_id=external_id, **mapped))
                file_ins += 1
            else:
                # outcome/outcome_at/repeats/hot live in raw and resolve later
                if existing.raw != mapped["raw"]:
                    existing.raw = mapped["raw"]
                    file_upd += 1
        db.commit()
        inserted += file_ins
        updated += file_upd
        report[str(path)] = {"rows": len(rows), "inserted": file_ins, "updated": file_upd}
    return {"inserted": inserted, "updated": updated, "files": report}


def sync_worker_signals(db: Session, *, force: bool = True) -> dict:
    """Ingest every per-ticker worker CSV (the raw engine signals) so ALL
    signals the service generates are stored, not just the merged central
    ledgers. Worker CSVs are append-only, so this pass is insert-only with
    a preloaded id set — cheap enough to run every minute.
    """
    settings = get_settings()
    try:
        cfg = read_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"inserted": 0, "files": {}, "error": "config unreadable"}
    inserted = 0
    report: dict[str, dict] = {}
    for cat_key, cat in (cfg.get("categories") or {}).items():
        for t in cat.get("tickers") or []:
            tid = t.get("id") or ""
            path = settings.super_dir / (t.get("path") or "") / (t.get("csv") or "")
            if not tid or not path.is_file() or not _file_changed(path, force=force):
                continue
            rows = _read_csv_rows(path)
            existing_ids = set(
                db.scalars(
                    select(SuperSignal.external_id).where(
                        SuperSignal.external_id.like(f"worker:{tid}:%")
                    )
                ).all()
            )
            file_ins = 0
            for row in rows:
                logged = (row.get("logged_at_cst") or "").strip()
                if not logged:
                    continue
                # engine/tf are absent in the legacy BTC worker header — the
                # empty strings still yield a stable, unique identity.
                external_id = (
                    f"worker:{tid}:{logged}:{(row.get('bar_time_cst') or '').strip()}:"
                    f"{(row.get('engine') or '').strip()}:{(row.get('tf') or '').strip()}:"
                    f"{(row.get('signal') or '')[:80]}"
                )
                if external_id in existing_ids:
                    continue
                existing_ids.add(external_id)
                db.add(
                    SuperSignal(
                        external_id=external_id,
                        book=(row.get("book") or "").strip() or None,
                        category=cat_key,
                        ticker=(t.get("label") or tid).upper(),
                        direction=(row.get("direction") or "").strip() or None,
                        grade=None,  # grading happens at ledger level (eng_hot)
                        combo=(row.get("signal") or "").strip() or None,
                        price=_float_or_none(row.get("signal_price")),
                        accuracy_pct=_float_or_none(row.get("acc_strict_pct")),
                        bar_time=(row.get("bar_time_cst") or "").strip() or None,
                        logged_at=_parse_cst(logged),
                        raw={k: v for k, v in row.items() if k},
                    )
                )
                file_ins += 1
            db.commit()
            inserted += file_ins
            report[str(path)] = {"rows": len(rows), "inserted": file_ins}
    return {"inserted": inserted, "files": report}


def sync_everything(db: Session, *, include_archive: bool = True, force: bool = True) -> dict:
    """One pass over every signal source: central ledgers, worker CSVs,
    gex/econ snapshots. Callers must hold no session-level locks; this
    serializes on _SYNC_LOCK itself."""
    with _SYNC_LOCK:
        return {
            "signals": sync_signals(db, include_archive=include_archive, force=force),
            "workers": sync_worker_signals(db, force=force),
            "snapshots": sync_snapshots(db),
        }


def sync_snapshots(db: Session) -> dict:
    """Upsert today's gex/econ JSON plus the per-day raw GEX archive into
    daily_snapshots (local file reads only — never touches flashAlpha)."""
    settings = get_settings()
    count = 0

    def upsert(kind: str, date: str, payload: dict, source: str) -> None:
        nonlocal count
        row = db.scalar(
            select(DailySnapshot).where(
                DailySnapshot.kind == kind, DailySnapshot.snapshot_date == date
            )
        )
        if row is None:
            db.add(
                DailySnapshot(
                    kind=kind, snapshot_date=date, payload=payload, source_file=source
                )
            )
            count += 1
        elif row.payload != payload:
            row.payload = payload
            row.source_file = source
            count += 1

    gex = read_gex()
    if gex and gex.get("fetched_at"):
        upsert("gex", str(gex["fetched_at"])[:10], gex, "gex_daily.json")
    econ = read_econ()
    if econ and econ.get("date"):
        upsert("econ", str(econ["date"]), econ, "econ_today.json")
    gex_dir = settings.super_dir / "gex"
    if gex_dir.is_dir():
        for f in sorted(gex_dir.glob("*.json")):
            # files are named <YYYY-MM-DD>_<ticker>.json
            stem_parts = f.stem.split("_", 1)
            if len(stem_parts) != 2:
                continue
            date, ticker = stem_parts
            payload = read_json_file(f)
            if payload is not None:
                upsert(f"gex_raw_{ticker}", date, payload, f.name)
    db.commit()
    return {"upserted": count}


def query_signals(
    db: Session,
    *,
    book: str | None = None,
    category: str | None = None,
    ticker: str | None = None,
    grade_min: int | None = None,
    days: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[SuperSignal]]:
    from datetime import timedelta

    from sqlalchemy import func

    stmt = select(SuperSignal)
    if book:
        stmt = stmt.where(SuperSignal.book == book.upper())
    if category:
        stmt = stmt.where(SuperSignal.category == category)
    if ticker:
        stmt = stmt.where(SuperSignal.ticker == ticker.upper())
    if grade_min is not None:
        stmt = stmt.where(SuperSignal.grade.in_([str(g) for g in range(grade_min, 9)]))
    if days:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        stmt = stmt.where(SuperSignal.logged_at >= cutoff)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(SuperSignal.logged_at.desc().nullslast()).limit(limit).offset(offset)
    return total, list(db.scalars(stmt).all())
