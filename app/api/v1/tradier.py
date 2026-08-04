"""Tradier Platform endpoints: balance, chain preview, managed positions.

The bot here is an EXECUTOR, not a signal engine: the operator says
"SPY call, 50% of the account, delta 0.25-0.50" and the service picks the
contract, sizes it, buys, rests the TP on the venue and monitors the SL.
Multiple positions run side by side.

Environment follows the desk's paper gate: VIDURA_PAPER_ONLY=true pins every
client to Tradier's SANDBOX venue (its own token, its own account), so paper
cannot reach live money by construction.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.cloud import require_local_runtime
from app.core.database import get_db
from app.models import User
from app.models.tradier import TradierPosition
from app.services import credentials as creds_svc
from app.services import tradier_bot
from app.services.tradier_client import TradierError

router = APIRouter(prefix="/tradier", tags=["tradier"])


def _user_or_404(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return user


def _translated(exc: Exception) -> HTTPException:
    if isinstance(exc, tradier_bot.TradierBotError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, creds_svc.CredentialsError):
        return HTTPException(status_code=424, detail=str(exc))
    if isinstance(exc, TradierError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/balance", operation_id="getTradierBalance")
def balance(user_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """Account equity and option buying power — the sizing base."""
    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    try:
        return client.balances()
    except TradierError as exc:
        raise _translated(exc) from exc
    finally:
        client.close()


@router.get("/chain", operation_id="previewTradierChain")
def chain_preview(
    user_id: str = Query(...),
    symbol: str = Query(..., max_length=12),
    side: str = Query(default="call", pattern="^(call|put)$"),
    delta_min: float = Query(default=tradier_bot.DEFAULT_DELTA_MIN, gt=0, lt=1),
    delta_max: float = Query(default=tradier_bot.DEFAULT_DELTA_MAX, gt=0, le=1),
    expiration: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
) -> dict:
    """What WOULD be traded: the delta-band candidates and the pick.

    Exists so the desk can show the contract before any money moves — an
    executor that only reveals its choice after the fill is not operable.
    """
    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    try:
        exp = expiration or (client.expirations(symbol.upper()) or [None])[0]
        if exp is None:
            raise HTTPException(status_code=404, detail=f"no expirations for {symbol}")
        raw = client.chain(symbol.upper(), exp)
        band = []
        for o in raw:
            g = o.get("greeks") or {}
            d = g.get("delta")
            if d is None or (o.get("option_type") or "").lower() != side:
                continue
            if delta_min <= abs(float(d)) <= delta_max:
                band.append({
                    "occ_symbol": o.get("symbol"), "strike": o.get("strike"),
                    "bid": o.get("bid"), "ask": o.get("ask"),
                    "delta": round(abs(float(d)), 4),
                    "volume": o.get("volume"), "open_interest": o.get("open_interest"),
                })
        pick = tradier_bot.pick_contract(raw, side, delta_min, delta_max)
        return {
            "symbol": symbol.upper(), "expiration": exp, "side": side,
            "band": sorted(band, key=lambda x: x["delta"], reverse=True),
            "pick": (pick or {}).get("symbol"),
        }
    except TradierError as exc:
        raise _translated(exc) from exc
    finally:
        client.close()


class OpenRequest(BaseModel):
    user_id: str
    symbol: str = Field(..., max_length=12, examples=["SPY"])
    side: str = Field(..., pattern="^(call|put)$")
    buy_pct: float = Field(default=tradier_bot.DEFAULT_BUY_PCT, gt=0, le=100,
                           description="% of option buying power to spend")
    delta_min: float = Field(default=tradier_bot.DEFAULT_DELTA_MIN, gt=0, lt=1)
    delta_max: float = Field(default=tradier_bot.DEFAULT_DELTA_MAX, gt=0, le=1)
    tp_pct: float = Field(default=tradier_bot.DEFAULT_TP_PCT, gt=0, le=500,
                          description="sell when the premium is this % above entry")
    sl_pct: float = Field(default=tradier_bot.DEFAULT_SL_PCT, gt=0, lt=100,
                          description="cancel the TP and sell when the bid is this % below entry")
    expiration: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                   description="YYYY-MM-DD; omit for the nearest listed")


@router.post("/positions", operation_id="openTradierPosition")
def open_position(payload: OpenRequest, db: Session = Depends(get_db)) -> dict:
    """Pick by delta, size by balance %, buy, then manage TP/SL."""
    require_local_runtime("Placing a Tradier order")
    user = _user_or_404(db, payload.user_id)
    try:
        pos = tradier_bot.open_position(
            db, user,
            symbol=payload.symbol, side=payload.side, buy_pct=payload.buy_pct,
            delta_min=payload.delta_min, delta_max=payload.delta_max,
            tp_pct=payload.tp_pct, sl_pct=payload.sl_pct,
            expiration=payload.expiration,
        )
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    return _pos_out(pos)


@router.get("/positions", operation_id="listTradierPositions")
def list_positions(
    user_id: str = Query(...),
    status: str = Query(default="all",
                        pattern="^(all|active|pending|open|tp_filled|sl_sold|closed|failed)$"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    _user_or_404(db, user_id)
    stmt = select(TradierPosition).where(TradierPosition.user_id == user_id)
    if status == "active":
        stmt = stmt.where(TradierPosition.status.in_(tradier_bot.ACTIVE_STATUSES))
    elif status != "all":
        stmt = stmt.where(TradierPosition.status == status)
    rows = list(db.scalars(
        stmt.order_by(TradierPosition.opened_at.desc()).limit(limit)
    ).all())
    return {"total": len(rows), "items": [_pos_out(p) for p in rows]}


@router.post("/positions/sweep", operation_id="sweepTradierPositions")
def sweep(user_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """Run one monitor pass now — the same sweep the background loop runs.

    The desk calls this on refresh so what it renders is the venue's current
    truth, not the state as of the last 10-second tick.
    """
    require_local_runtime("Sweeping Tradier positions")
    user = _user_or_404(db, user_id)
    try:
        return tradier_bot.monitor_pass(db, user)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc


@router.post("/positions/{position_id}/close", operation_id="closeTradierPosition")
def close_position(position_id: int, user_id: str = Query(...),
                   db: Session = Depends(get_db)) -> dict:
    """Manual exit: cancel resting orders, sell at market."""
    require_local_runtime("Closing a Tradier position")
    user = _user_or_404(db, user_id)
    try:
        return _pos_out(tradier_bot.close_position(db, user, position_id))
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc


def _pos_out(p: TradierPosition) -> dict:
    return {
        "id": p.id, "status": p.status, "sandbox": p.sandbox,
        "underlying": p.underlying, "occ_symbol": p.occ_symbol,
        "option_type": p.option_type, "strike": p.strike,
        "expiration": p.expiration, "delta_at_entry": p.delta_at_entry,
        "contracts": p.contracts, "entry_price": p.entry_price,
        "tp_price": p.tp_price, "sl_price": p.sl_price,
        "tp_pct": p.tp_pct, "sl_pct": p.sl_pct, "buy_pct": p.buy_pct,
        "exit_price": p.exit_price, "pnl_usd": p.pnl_usd,
        "note": p.note,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
    }
