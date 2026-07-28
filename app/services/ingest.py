"""Ingest bot-written trade CSVs into the unified SQLite trade ledger.

Each bot family writes its own CSV shape; this module maps them into the
``trades`` table.  Files remain bot-owned (read-only here) — ingest upserts
on a deterministic external_id so re-running is idempotent, and sports rows
that flip OPEN -> CLOSED in place update the stored row.

Caveat inherited from forensics (2026-07-16): legacy sports CSV
``realized_pnl`` before 07/16 is overstated ("phantom"); true P&L comes from
the Kalshi fills+settlements API.  Rows are stored as recorded with the full
original row preserved in ``raw``.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import paths
from app.core.config import get_settings
from app.models import Trade, User

logger = logging.getLogger(__name__)

_CST = ZoneInfo("America/Chicago")


def _parse_ts(value: str, fmts: tuple[str, ...], tz=_CST) -> datetime | None:
    value = (value or "").strip().replace(" CST", "").replace(" CDT", "")
    for fmt in fmts:
        try:
            # Stored naive-UTC to match the rest of the ledger.
            aware = datetime.strptime(value, fmt).replace(tzinfo=tz)
            return aware.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _f(value, default=None) -> float | None:
    try:
        return float(str(value).replace("$", "").strip())
    except (TypeError, ValueError):
        return default


def _i(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _read_rows(path: Path) -> list[dict]:
    """Tolerant DictReader: ragged extra columns land under '_extra'."""
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh, restkey="_extra")
            return [row for row in reader if any((v or "").strip() for v in row.values() if isinstance(v, str))]
    except OSError as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return []


def _clean_raw(row: dict) -> dict:
    return {str(k): (v if isinstance(v, (str, int, float, bool, list)) else str(v)) for k, v in row.items() if k}


# --- per-family row mappers -----------------------------------------------

def _map_btc15_v2(row: dict, version: str) -> dict | None:
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        return None
    opened = _parse_ts(row.get("timestamp", ""), ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"))
    pnl = _f(row.get("pnl"))
    status = "settled"
    if pnl is not None:
        status = "won" if pnl > 0 else ("lost" if pnl < 0 else "settled")
    return {
        "bot_key": "btc15",
        "bot_version": version,
        "external_id": f"btc15:{version}:{row.get('timestamp', '')}:{ticker}",
        "ticker": ticker,
        "side": (row.get("direction") or "").strip().lower() or None,
        "action": "buy",
        "contracts": _i(row.get("contracts")),
        "price_cents": _f(row.get("entry_price")),
        "pnl_usd": pnl,
        "status": status,
        # The CSV 'mode' column records the entry style (BUY/FLIP-BUY), not
        # paper-vs-live, so we cannot infer live trades from it.
        "is_mock": True,
        "opened_at": opened,
        "closed_at": opened,
        "raw": _clean_raw(row),
    }


def _map_btc15_v5(row: dict) -> dict | None:
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        return None
    action = (row.get("action") or "buy").strip().lower()
    opened = _parse_ts(row.get("timestamp_ct", ""), ("%m/%d/%Y %H:%M:%S",))
    return {
        "bot_key": "btc15",
        "bot_version": "v5",
        "external_id": f"btc15:v5:{row.get('timestamp_ct', '')}:{ticker}:{action}",
        "ticker": ticker,
        "side": (row.get("side") or "").strip().lower() or None,
        "action": action if action in ("buy", "sell") else "buy",
        "contracts": _i(row.get("contracts")),
        "price_cents": _f(row.get("price_cents")),
        "status": "closed" if action == "sell" else "open",
        "is_mock": (row.get("dry_run") or "").strip().upper() in ("TRUE", "1", "YES"),
        "opened_at": opened,
        "raw": _clean_raw(row),
    }


def _map_btc60(row: dict, version: str, *, is_paper: bool) -> dict | None:
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        return None
    opened = _parse_ts(
        row.get("timestamp", ""),
        ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"),
        tz=timezone.utc,
    )
    pnl = _f(row.get("pnl_dollars"))
    status = "settled"
    if pnl is not None:
        status = "won" if pnl > 0 else ("lost" if pnl < 0 else "settled")
    entry = _f(row.get("entry_cents"))
    contracts = _i(row.get("contracts"))
    return {
        "bot_key": "btc60",
        "bot_version": version,
        "external_id": f"btc60:{version}:{row.get('timestamp', '')}:{ticker}",
        "ticker": ticker,
        "side": (row.get("side") or "").strip().lower() or None,
        "action": "buy",
        "contracts": contracts,
        "price_cents": entry,
        "cost_usd": round(entry * contracts / 100, 2) if entry is not None else None,
        "pnl_usd": pnl,
        "status": status,
        "is_mock": is_paper,
        "opened_at": opened,
        "closed_at": opened,
        "raw": _clean_raw(row),
    }


def _map_sports(row: dict, *, is_paper: bool, source: str) -> dict | None:
    ticker = (row.get("ticker") or "").strip()
    ts_epoch = (row.get("ts_epoch") or "").strip()
    if not ticker or not ts_epoch:
        return None
    opened = None
    epoch = _f(ts_epoch)
    if epoch:
        opened = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    status_raw = (row.get("status") or "").strip().upper()
    pnl = _f(row.get("realized_pnl"))
    if status_raw == "OPEN":
        status = "open"
    elif pnl is not None and pnl != 0:
        status = "won" if pnl > 0 else "lost"
    else:
        status = "closed"
    closed = (
        _parse_ts(
            row.get("ts_close", ""),
            ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"),
        )
        if status != "open"
        else None
    )
    fill = _f(row.get("fill_price")) or _f(row.get("buy_price"))
    contracts = _i(row.get("contracts"))
    return {
        "bot_key": "sports",
        "bot_version": source,
        # ts_epoch is the bots' row key, but real ledgers contain duplicate
        # ts_epoch values for distinct trades, so ticker joins the key and
        # sync_trades adds an occurrence suffix for exact repeats. The key
        # must NOT include the source tag: the same ledger can be visible
        # under two filenames (trade_history.csv vs trade_history_sports.csv).
        "external_id": f"sports:{ts_epoch}:{ticker}",
        "ticker": ticker,
        "market_title": (row.get("name") or row.get("player") or row.get("team") or "").strip() or None,
        "side": "yes",  # sports bots only ever buy YES
        "action": "buy",
        "contracts": contracts,
        "price_cents": fill,
        "cost_usd": round(fill * contracts / 100, 2) if fill is not None else None,
        "pnl_usd": pnl,
        "status": status,
        "is_mock": is_paper,
        "opened_at": opened,
        "closed_at": closed,
        "raw": _clean_raw(row),
    }


# --- file discovery + upsert ----------------------------------------------

def _candidate_files(user: User, bot_key: str) -> list[tuple[Path, str]]:
    """Return (file, parser_tag) pairs that may exist for this user/bot."""
    root = paths.normalize_root(user.user_root_folder)
    trade_dir = root / "trade_history"
    out: list[tuple[Path, str]] = []
    if bot_key == "btc15":
        for ver in ("v2", "v3", "v4"):
            out.append((trade_dir / f"{ver}_trade_history.csv", f"btc15_{ver}"))
        out.append((root / f"v2_trade_history.csv", "btc15_v2"))
        out.append((trade_dir / "bot_btc_15_2_trades.csv", "btc15_v5"))
        out.append((root / "bot_btc_15_2_trades.csv", "btc15_v5"))
    elif bot_key == "btc60":
        # btc60 writes next to its script (shared across users by design).
        btc60_dir = get_settings().source_repo / "prediction-trade/kalshi/btc/btc60"
        for ver in ("fable5", "burst"):
            out.append((btc60_dir / f"btc60_{ver}_trade_history_paper.csv", f"btc60_{ver}_paper"))
            out.append((btc60_dir / f"btc60_{ver}_trade_history.csv", f"btc60_{ver}_live"))
    elif bot_key == "sports":
        out.append((trade_dir / "trade_history_main.csv", "sports_main_live"))
        out.append((trade_dir / "paper_trades_main.csv", "sports_main_paper"))
        out.append((trade_dir / "trade_history_sports.csv", "sports_v1_live"))
        out.append((trade_dir / "trade_history.csv", "sports_v1_live"))
        out.append((trade_dir / "trade_history_baseball.csv", "sports_v2_live"))
    return [(p, tag) for p, tag in out if p.is_file()]


def _map_row(tag: str, row: dict) -> dict | None:
    if tag.startswith("btc15_v5"):
        return _map_btc15_v5(row)
    if tag.startswith("btc15_"):
        return _map_btc15_v2(row, tag.split("_")[1])
    if tag.startswith("btc60_"):
        _, version, mode = tag.split("_")
        return _map_btc60(row, version, is_paper=(mode == "paper"))
    if tag.startswith("sports_"):
        _, source, mode = tag.split("_")
        return _map_sports(row, is_paper=(mode == "paper"), source=source)
    return None


def sync_trades(db: Session, user: User, bot_key: str) -> dict:
    """Idempotent CSV -> SQLite sync. Returns counts per file."""
    report: dict[str, dict] = {}
    inserted = updated = 0
    for path, tag in _candidate_files(user, bot_key):
        rows = _read_rows(path)
        file_ins = file_upd = 0
        seen: dict[str, int] = {}
        for row in rows:
            mapped = _map_row(tag, row)
            if mapped is None:
                continue
            # Row order in the bot CSVs is stable (in-place updates, appends
            # at the end), so an occurrence index disambiguates real
            # duplicate keys deterministically across re-syncs.
            base_id = mapped["external_id"]
            n = seen[base_id] = seen.get(base_id, 0) + 1
            if n > 1:
                mapped["external_id"] = f"{base_id}#{n}"
            existing = db.scalar(
                select(Trade).where(
                    Trade.user_id == user.user_id,
                    Trade.external_id == mapped["external_id"],
                )
            )
            if existing is None:
                if mapped.get("opened_at") is None:
                    mapped.pop("opened_at", None)
                db.add(Trade(user_id=user.user_id, **mapped))
                file_ins += 1
            else:
                # Sports CSVs update rows in place (OPEN -> CLOSED). Never
                # downgrade a settled row back to open — a stale copy of the
                # same ledger under another filename must not erase P&L.
                if mapped.get("status") == "open" and existing.status != "open":
                    continue
                changed = False
                for field in ("status", "pnl_usd", "closed_at", "price_cents", "cost_usd", "raw"):
                    new = mapped.get(field)
                    if new is not None and getattr(existing, field) != new:
                        setattr(existing, field, new)
                        changed = True
                if changed:
                    file_upd += 1
        db.commit()
        inserted += file_ins
        updated += file_upd
        report[str(path)] = {"rows": len(rows), "inserted": file_ins, "updated": file_upd}
    return {"bot_key": bot_key, "inserted": inserted, "updated": updated, "files": report}
