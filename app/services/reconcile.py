"""Settle ledger rows that are still ``open`` but no longer open on Kalshi.

A bot can miss its own close: it crashes mid-exit, the market settles while it
is between cycles, or a fire-sell fills after the process is gone. The row then
sits at ``open`` forever, its cost counted and its P&L absent — and the CSV
close-fallback may later invent a sparse CLOSED row for it.

This reconciles against the exchange, which is the only authority on what
actually happened:

    stale open row  ->  is the ticker still an ACTIVE Kalshi position?
                        yes -> leave it alone, the bot is simply still in it
                        no  -> it resolved; take the true P&L from
                               fills + settlements and close the row

"True P&L" matters here. The bots' own ``realized_pnl`` estimates are the
figures a 2026-07-16 forensic pass found overstated by roughly $8k; fills and
settlements are what the account really did. The arithmetic below mirrors the
sports bot's own ``_pnl_from_fills`` / ``_settlement_revenue`` so the two
cannot drift apart:

    fill:        sell -> +(count x price - fee),  buy -> -(count x price + fee)
    settlement:  + revenue/100

Priced on the token we HELD (yes_price for a yes position, no_price for a no).
A fill's own ``side`` field describes order matching, not our holding, so using
it would silently mis-price every ``no`` trade.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Trade, User
from app.services import credentials, kalshi_client

logger = logging.getLogger(__name__)

# Every family whose rows live on the Kalshi account.
BOT_KEYS = ("btc15", "btc60", "btcperp", "sports")

# A position younger than this is not suspicious — the bot is probably just
# holding it. The window is the whole point of the check: it separates "still
# running" from "nobody is coming back for this".
DEFAULT_STALE_HOURS = 24


class ReconcileError(RuntimeError):
    """The exchange could not be reached, so nothing was decided."""


def _epoch(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def stale_open(db: Session, user: User, *, hours: int = DEFAULT_STALE_HOURS) -> list[Trade]:
    """Live rows still ``open`` after ``hours``, newest first.

    Paper rows are excluded on purpose: they never reached the exchange, so
    Kalshi has no fills to price them with and "not an active position" would
    be true of every single one — reconciling them would zero the lot.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    stmt = (
        select(Trade)
        .where(
            Trade.user_id == user.user_id,
            Trade.bot_key.in_(BOT_KEYS),
            Trade.status == "open",
            Trade.is_live.is_(True),
            Trade.opened_at < cutoff,
        )
        .order_by(Trade.opened_at.desc())
    )
    return list(db.scalars(stmt).all())


def _pnl_from_fills(client, ticker: str, side: str | None, since_ts: float) -> tuple[float, int]:
    """Realized P&L from this ticker's fills, and how many were counted."""
    key = "no_price_dollars" if (side or "yes").lower() == "no" else "yes_price_dollars"
    pnl, seen = 0.0, 0
    for fx in client.fills(ticker=ticker, limit=1000):
        if ticker not in (fx.get("ticker"), fx.get("market_ticker")):
            continue
        # 5s of slack: the fill that OPENED the trade can be stamped a moment
        # before the row's own opened_at, and dropping it would count the exit
        # without its entry — turning a small loss into a large phantom gain.
        if since_ts and float(fx.get("ts") or 0) < since_ts - 5:
            continue
        cnt = float(fx.get("count_fp") or fx.get("count") or 0)
        px = float(fx.get(key) or 0)
        fee = float(fx.get("fee_cost") or 0)
        pnl += (cnt * px - fee) if fx.get("action") == "sell" else -(cnt * px + fee)
        seen += 1
    return round(pnl, 2), seen


def _settlement_revenue(client, ticker: str, settlements: list[dict]) -> tuple[float, int]:
    rev, seen = 0.0, 0
    for s in settlements:
        if ticker in (s.get("ticker"), s.get("market_ticker"), s.get("event_ticker")):
            rev += float(s.get("revenue") or 0) / 100.0
            seen += 1
    return round(rev, 2), seen


def reconcile(
    db: Session,
    user: User,
    *,
    hours: int = DEFAULT_STALE_HOURS,
    dry_run: bool = True,
) -> dict:
    """Close stale open rows the exchange says are finished.

    Defaults to ``dry_run`` — this rewrites booked P&L, so producing the plan
    and applying it are separate decisions.
    """
    candidates = stale_open(db, user, hours=hours)
    if not candidates:
        return {"checked": 0, "active": 0, "resolved": [], "updated": 0,
                "dry_run": dry_run, "hours": hours}

    try:
        creds = credentials.load_kalshi_credentials(user.user_root_folder)
        client = kalshi_client.KalshiClient(
            creds.api_key_id, creds.private_key_path, creds.base_uri
        )
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        raise ReconcileError(f"cannot reach Kalshi: {exc}") from exc

    try:
        # One positions call for the whole sweep. A ticker with a non-zero
        # position is still live and is left strictly alone.
        active = {
            p.get("ticker") for p in client.positions(limit=1000)
            if int(p.get("position") or 0) != 0
        }
        # Settlements are account-wide and not ticker-filterable on this
        # client, so fetch once and match locally rather than per row.
        settlements = client.settlements(limit=1000)

        resolved, still_open = [], 0
        for t in candidates:
            if t.ticker in active:
                still_open += 1
                continue
            fills_pnl, n_fills = _pnl_from_fills(client, t.ticker, t.side, _epoch(t.opened_at))
            sett_rev, n_sett = _settlement_revenue(client, t.ticker, settlements)
            if n_fills == 0 and n_sett == 0:
                # No exchange record at all. Could be a paper row mislabelled
                # live, or a ticker outside this account's history. Refuse to
                # invent a $0 settlement for it.
                resolved.append({
                    "id": t.id, "ticker": t.ticker, "bot_key": t.bot_key,
                    "opened_at": str(t.opened_at), "action": "skipped",
                    "reason": "no fills or settlements on this ticker",
                })
                continue
            pnl = round(fills_pnl + sett_rev, 2)
            status = "won" if pnl > 0 else ("lost" if pnl < 0 else "closed")
            resolved.append({
                "id": t.id, "ticker": t.ticker, "bot_key": t.bot_key,
                "opened_at": str(t.opened_at),
                "old_status": t.status, "old_pnl": t.pnl_usd,
                "new_status": status, "new_pnl": pnl,
                "fills_pnl": fills_pnl, "settlement_revenue": sett_rev,
                "fills": n_fills, "settlements": n_sett,
                "action": "would_update" if dry_run else "updated",
            })
            if dry_run:
                continue
            t.status = status
            t.pnl_usd = pnl
            t.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            # Stamped so this row is never mistaken for a bot-reported figure.
            raw = dict(t.raw or {})
            raw["reconciled"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "source": "kalshi fills+settlements",
                "fills_pnl": fills_pnl,
                "settlement_revenue": sett_rev,
                "replaced_status": "open",
            }
            t.raw = raw
        if not dry_run:
            db.commit()
    finally:
        client.close()

    updated = sum(1 for r in resolved if r["action"] == "updated")
    logger.info("reconcile: %s stale, %s still active, %s resolved, %s written",
                len(candidates), still_open, len(resolved), updated)
    return {
        "checked": len(candidates),
        "active": still_open,
        "resolved": resolved,
        "updated": updated,
        "dry_run": dry_run,
        "hours": hours,
    }
