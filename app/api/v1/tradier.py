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


@router.get("/venue", operation_id="getTradierVenue")
def venue(user_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """Which venues this operator can reach, for the desk's LIVE toggle.

    Reports configuration only — no order can be placed from here, and the
    tokens themselves never leave the server.
    """
    user = _user_or_404(db, user_id)
    from app.core.config import get_settings

    out = {"paper_only_server": get_settings().paper_only,
           "sandbox": None, "live": None}
    for key, is_live in (("sandbox", False), ("live", True)):
        try:
            creds = creds_svc.load_tradier_credentials(
                user.user_root_folder, sandbox=not is_live)
            out[key] = {"configured": True, "base_url": creds.base_url,
                        "account_id": creds.account_id}
        except Exception as exc:                      # noqa: BLE001
            out[key] = {"configured": False, "reason": str(exc)}
    if out["live"]["configured"] and get_settings().paper_only:
        out["live"]["reason"] = "server locked to paper (VIDURA_PAPER_ONLY)"
    return out


@router.get("/balance", operation_id="getTradierBalance")
def balance(user_id: str = Query(...),
            live: bool = Query(default=False,
                               description="true reads the PRODUCTION account"),
            db: Session = Depends(get_db)) -> dict:
    """Account equity and option buying power — the sizing base."""
    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user, live=live)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    try:
        return client.balances()
    except TradierError as exc:
        raise _translated(exc) from exc
    finally:
        client.close()


# The desk's ticker rail, in the operator's fixed display order.
DESK_TICKERS = ("SPX,SPY,QQQ,VIX,IWM,GLD,AAPL,TSLA,NVDA,MSFT,AMZN,MU,SNDK,"
                "AVGO,META,GOOGL,LLY,JPM,ORCL,IBM,ONDS,IONQ,QBTS")

# One shared quotes cache: the rail polls every 15s per browser tab, Tradier
# rate-limits per token — 10s TTL keeps N tabs at one upstream call per tick.
_QUOTES_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTES_TTL = 10.0


def _tradier_quote_out(q: dict) -> dict:
    def f(key):
        v = q.get(key)
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None
    return {
        "symbol": (q.get("symbol") or "").upper(),
        "price": f("last"),
        "prev_close": f("prevclose"),
        "change": f("change"),
        "change_pct": f("change_percentage"),
        "source": "tradier",
    }


# The index strip. Tradier quotes SPX and VIX as indices directly, but has no
# symbol for the Dow itself (DJI is unmatched) — DIA, the SPDR Dow ETF, is the
# tradable proxy, labelled DOW so the strip reads the way an operator expects.
STREAM_SYMBOLS = [
    {"label": "SPX", "symbol": "SPX"},
    {"label": "SPY", "symbol": "SPY"},
    {"label": "DOW", "symbol": "DIA"},
    {"label": "QQQ", "symbol": "QQQ"},
    {"label": "VIX", "symbol": "VIX"},
]


@router.post("/stream/session", operation_id="createTradierStreamSession")
def stream_session(user_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    """Short-lived credential for Tradier's market WebSocket, plus a seed snapshot.

    Streaming is PRODUCTION-ONLY — the sandbox token is rejected with
    "Required scope(s): scope-stream", so this always uses the live keys. That
    is safe and deliberate: a market session can only read quotes, never place
    an order, and paper trading against real prices is the point.

    The account token never reaches the browser. What is returned is Tradier's
    session id, which is market-data-only and must be connected within five
    minutes; the page reconnects by asking for a fresh one.
    """
    require_local_runtime("Opening a Tradier market stream")
    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user, live=True)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    try:
        data = client.market_session()
        seed = []
        try:
            wanted = [s["symbol"] for s in STREAM_SYMBOLS]
            by_symbol = {(q.get("symbol") or "").upper(): q
                         for q in client.quotes(wanted)}
            for s in STREAM_SYMBOLS:
                q = by_symbol.get(s["symbol"])
                seed.append({**s, **(_tradier_quote_out(q) if q else {}),
                             "symbol": s["symbol"], "label": s["label"]})
        except TradierError:
            seed = [dict(s) for s in STREAM_SYMBOLS]
        return {
            "sessionid": data.get("sessionid"),
            "url": data.get("url"),
            "ws_url": "wss://ws.tradier.com/v1/markets/events",
            "symbols": STREAM_SYMBOLS,
            "seed": seed,
            "venue": "live",
        }
    except TradierError as exc:
        raise _translated(exc) from exc
    finally:
        client.close()


# symbol|interval|venue -> (monotonic, payload). Two charts polling at once
# must not become two upstream calls per tick.
_BARS_CACHE: dict[str, tuple[float, dict]] = {}
_BARS_TTL = 25.0


@router.get("/timesales", operation_id="getTradierTimesales")
def timesales(
    user_id: str = Query(...),
    symbol: str = Query(..., max_length=12),
    interval: str = Query(default="1min", pattern="^(1min|5min|15min)$"),
    live: bool = Query(default=False,
                       description="true reads bars from the PRODUCTION venue"),
    db: Session = Depends(get_db),
) -> dict:
    """Today's intraday bars, plus the prior close the day is measured against.

    Seeds the desk's charts: a WebSocket only produces from the moment it
    connects, so without this the chart would start blank every reload.
    """
    import time as _t

    sym = symbol.strip().upper()
    key = f"{sym}|{interval}|{'live' if live else 'sbx'}"
    hit = _BARS_CACHE.get(key)
    if hit and _t.monotonic() - hit[0] < _BARS_TTL:
        return hit[1]

    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user, live=live)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    try:
        raw = client.timesales(sym, interval=interval)
        bars = []
        for b in raw:
            try:
                bars.append({
                    "t": int(b.get("timestamp") or 0),
                    "time": b.get("time"),
                    "o": float(b.get("open") or 0),
                    "h": float(b.get("high") or 0),
                    "l": float(b.get("low") or 0),
                    "c": float(b.get("close") or b.get("price") or 0),
                    "v": float(b.get("volume") or 0),
                })
            except (TypeError, ValueError):
                continue
        prev_close = None
        try:
            q = (client.quotes([sym]) or [{}])[0]
            prev_close = float(q.get("prevclose")) if q.get("prevclose") else None
        except (TradierError, TypeError, ValueError, IndexError):
            prev_close = None
        out = {"symbol": sym, "interval": interval, "bars": bars,
               "prev_close": prev_close, "venue": "live" if live else "sandbox"}
        _BARS_CACHE[key] = (_t.monotonic(), out)
        return out
    except TradierError as exc:
        raise _translated(exc) from exc
    finally:
        client.close()


@router.get("/flow", operation_id="getTradierOptionsFlow")
def options_flow(
    user_id: str = Query(...),
    live: bool = Query(default=False,
                       description="true reads chains from the PRODUCTION venue"),
    refresh: bool = Query(default=False, description="force a sweep now"),
    db: Session = Depends(get_db),
) -> dict:
    """Top option contracts by volume across the large-cap universe.

    Returns the last good snapshot immediately and refreshes in the
    background when it is stale — a 50-name chain sweep takes seconds and
    must never sit in front of the desk's render.
    """
    from app.services import options_flow as flow_svc

    user = _user_or_404(db, user_id)
    try:
        return flow_svc.snapshot(user, live=live, force=refresh)
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc


@router.get("/quotes", operation_id="getTradierQuotes")
def desk_quotes(
    user_id: str = Query(...),
    symbols: str = Query(default=DESK_TICKERS, max_length=500),
    live: bool = Query(default=False,
                       description="true quotes from the PRODUCTION venue"),
    db: Session = Depends(get_db),
) -> dict:
    """Live prices for the desk's ticker rail, in the requested order.

    Tradier batch quotes first (one request for the whole rail); any symbol
    Tradier does not return — indices on the sandbox, or the whole set when
    the account has no Tradier keys yet — is filled from a one-call yfinance
    batch, so the rail renders before the operator ever adds keys.
    """
    from app.services import quotes as quotes_svc

    syms, seen = [], set()
    for s in symbols.split(","):
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            syms.append(s)
    if not syms:
        raise HTTPException(status_code=400, detail="no symbols given")

    cache_key = f"{user_id}:{'live' if live else 'sbx'}:{','.join(syms)}"
    import time as _t
    hit = _QUOTES_CACHE.get(cache_key)
    if hit and _t.monotonic() - hit[0] < _QUOTES_TTL:
        return hit[1]

    user = _user_or_404(db, user_id)
    found: dict[str, dict] = {}
    sources = set()
    try:
        client = tradier_bot.client_for(user, live=live)
        try:
            for q in client.quotes(syms):
                out = _tradier_quote_out(q)
                if out["symbol"] and out["price"] is not None:
                    found[out["symbol"]] = out
                    sources.add("tradier")
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — no keys / venue down: yfinance covers
        pass

    missing = [s for s in syms if s not in found]
    if missing:
        try:
            for q in quotes_svc.batch_quotes(missing):
                if q.get("price") is not None:
                    found[q["symbol"]] = q
                    sources.add("yfinance")
        except Exception:  # noqa: BLE001 — a dead rail beats a 502 desk
            pass

    result = {
        "source": "+".join(sorted(sources)) or "none",
        "quotes": [found.get(s, {"symbol": s, "price": None, "prev_close": None,
                                 "change": None, "change_pct": None,
                                 "source": "none"}) for s in syms],
    }
    _QUOTES_CACHE[cache_key] = (_t.monotonic(), result)
    return result


@router.get("/chain", operation_id="previewTradierChain")
def chain_preview(
    user_id: str = Query(...),
    symbol: str = Query(..., max_length=12),
    side: str = Query(default="call", pattern="^(call|put)$"),
    delta_min: float = Query(default=tradier_bot.DEFAULT_DELTA_MIN, gt=0, lt=1),
    delta_max: float = Query(default=tradier_bot.DEFAULT_DELTA_MAX, gt=0, le=1),
    expiration: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    live: bool = Query(default=False,
                       description="true prices from the PRODUCTION venue"),
    db: Session = Depends(get_db),
) -> dict:
    """What WOULD be traded: the delta-band candidates and the pick.

    Exists so the desk can show the contract before any money moves — an
    executor that only reveals its choice after the fill is not operable.
    """
    user = _user_or_404(db, user_id)
    try:
        client = tradier_bot.client_for(user, live=live)
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
    live: bool = Field(default=False,
                       description="false (default) places a MOCK order on the "
                                   "Tradier sandbox; true spends real money on "
                                   "the production account")


@router.post("/positions", operation_id="openTradierPosition")
def open_position(payload: OpenRequest, db: Session = Depends(get_db)) -> dict:
    """Pick by delta, size by balance %, buy, then manage TP/SL.

    Sandbox unless ``live`` is explicitly true — the desk's LIVE toggle is the
    only thing that routes an order to the production account.
    """
    require_local_runtime("Placing a Tradier order")
    user = _user_or_404(db, payload.user_id)
    try:
        pos = tradier_bot.open_position(
            db, user,
            symbol=payload.symbol, side=payload.side, buy_pct=payload.buy_pct,
            delta_min=payload.delta_min, delta_max=payload.delta_max,
            tp_pct=payload.tp_pct, sl_pct=payload.sl_pct,
            expiration=payload.expiration, live=payload.live,
        )
    except Exception as exc:                          # noqa: BLE001
        raise _translated(exc) from exc
    return _pos_out(pos)


@router.get("/positions", operation_id="listTradierPositions")
def list_positions(
    user_id: str = Query(...),
    status: str = Query(default="all",
                        pattern="^(all|active|pending|open|tp_filled|sl_sold|closed|failed)$"),
    venue: str = Query(default="all", pattern="^(all|live|sandbox)$",
                       description="filter by the venue the position was opened on"),
    marks: bool = Query(default=False,
                        description="attach live bid + unrealized P&L for active rows"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    user = _user_or_404(db, user_id)
    stmt = select(TradierPosition).where(TradierPosition.user_id == user_id)
    if status == "active":
        stmt = stmt.where(TradierPosition.status.in_(tradier_bot.ACTIVE_STATUSES))
    elif status != "all":
        stmt = stmt.where(TradierPosition.status == status)
    if venue == "live":
        stmt = stmt.where(TradierPosition.sandbox.is_(False))
    elif venue == "sandbox":
        stmt = stmt.where(TradierPosition.sandbox.is_(True))
    rows = list(db.scalars(
        stmt.order_by(TradierPosition.opened_at.desc()).limit(limit)
    ).all())
    quotes: dict[str, dict] = {}
    if marks and rows:
        quotes = tradier_bot.live_quotes(user, rows)

    def mark_for(p: TradierPosition) -> dict | None:
        # Keyed by contract, and two rows can hold the SAME contract — so a
        # settled row must not borrow the open row's quote and report a live
        # P&L next to its realized one.
        if p.status not in tradier_bot.ACTIVE_STATUSES:
            return None
        return quotes.get(p.occ_symbol)

    return {"total": len(rows),
            "items": [_pos_out(p, mark_for(p)) for p in rows]}


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


# ── auto-trade: opening-range level-cross watcher ───────────────────────────

class AutoTradeStart(BaseModel):
    """Omitted fields fall back to Settings (env: VIDURA_TRADIER_*)."""

    user_id: str
    strategy: str | None = Field(default=None, examples=["10min_intraday_move"])
    tickers: str | None = Field(default=None, max_length=120,
                                description="comma-separated, e.g. SPY,QQQ,SPX")
    window_open: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$",
                                    description="HH:MM CST")
    window_close: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$",
                                     description="HH:MM CST")
    buy_pct: float | None = Field(default=None, gt=0, le=100)
    tp_pct: float | None = Field(default=None, gt=0, le=500)
    sl_pct: float | None = Field(default=None, gt=0, lt=100)
    delta_min: float | None = Field(default=None, gt=0, lt=1)
    delta_max: float | None = Field(default=None, gt=0, le=1)
    min_contracts: int | None = Field(default=None, ge=1, le=1000)
    # --- ab_signal_options only ---
    books: str | None = Field(default=None, examples=["A,B"],
                              description="which super-signal books to act on")
    dte_max: int | None = Field(default=None, ge=0, le=30,
                                description="furthest expiration to consider, in days")
    zero_dte_cutoff: str | None = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="CST time after which same-day expirations are not entered",
        examples=["13:00"])
    cooldown_min: int | None = Field(
        default=None, ge=0, le=1440,
        description="minutes this strategy must wait before re-entering the "
                    "same ticker (0 disables)",
        examples=[60])
    live: bool = Field(default=False,
                       description="false (default) arms the watcher on the "
                                   "SANDBOX venue; true trades real money")


@router.post("/autotrade/start", operation_id="startTradierAutoTrade")
def autotrade_start(payload: AutoTradeStart, db: Session = Depends(get_db)) -> dict:
    """Arm the opening-range auto-trader for this user.

    Watches the configured tickers' level crosses stamped inside the CST
    window; a new above_10min_high (CALL) / below_10min_low (PUT) must still
    hold after the confirmation delay, then the desk's normal managed 0DTE
    position is opened — TP rests on the venue, SL is the monitor loop.
    Sizing below min_contracts skips the trade.
    """
    from app.services import auto_trade

    require_local_runtime("Arming the auto-trader")
    _user_or_404(db, payload.user_id)
    try:
        return auto_trade.start(
            payload.user_id,
            strategy=payload.strategy, tickers=payload.tickers,
            window_open=payload.window_open, window_close=payload.window_close,
            buy_pct=payload.buy_pct, tp_pct=payload.tp_pct, sl_pct=payload.sl_pct,
            delta_min=payload.delta_min, delta_max=payload.delta_max,
            min_contracts=payload.min_contracts, live=payload.live,
            books=payload.books, dte_max=payload.dte_max,
            zero_dte_cutoff=payload.zero_dte_cutoff,
            cooldown_min=payload.cooldown_min,
        )
    except auto_trade.AutoTradeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/autotrade/stop", operation_id="stopTradierAutoTrade")
def autotrade_stop(user_id: str = Query(...)) -> dict:
    from app.services import auto_trade

    require_local_runtime("Stopping the auto-trader")
    return auto_trade.stop(user_id)


@router.get("/autotrade/status", operation_id="getTradierAutoTradeStatus")
def autotrade_status(user_id: str = Query(...)) -> dict:
    from app.services import auto_trade

    return auto_trade.status(user_id)


def _limit_price(p: TradierPosition) -> float | None:
    """The limit the buy was placed at — the ask when the contract was picked."""
    raw = p.raw if isinstance(p.raw, dict) else {}
    try:
        return float((raw.get("picked") or {}).get("ask"))
    except (TypeError, ValueError):
        return None


def _pos_out(p: TradierPosition, quote: dict | None = None) -> dict:
    # TP/SL are percentages OF THE FILL, so they cannot be final until the
    # buy fills. While it is still working, show what they would be at the
    # limit price and mark them provisional — blank cells read as "stale"
    # when the truth is "this order has not filled yet".
    tp_price, sl_price = p.tp_price, p.sl_price
    provisional = False
    if p.status == "pending" and p.entry_price is None and tp_price is None:
        limit = _limit_price(p)
        if limit:
            tp_price, sl_price = tradier_bot.exit_prices(limit, p.tp_pct, p.sl_pct)
            provisional = True

    # Unrealized mark and P&L for a position that is still running. Priced on
    # the BID: that is what closing it right now would actually pay.
    live_bid = live_pnl = None
    if quote is not None:
        try:
            live_bid = float(quote.get("bid")) if quote.get("bid") is not None else None
        except (TypeError, ValueError):
            live_bid = None
        if live_bid is not None and p.entry_price and p.contracts:
            live_pnl = round((live_bid - p.entry_price) * 100 * p.contracts, 2)
    return {
        "id": p.id, "status": p.status, "sandbox": p.sandbox,
        "live_bid": live_bid, "live_pnl_usd": live_pnl,
        "strategy": p.strategy or "Manual",
        "underlying": p.underlying, "occ_symbol": p.occ_symbol,
        "option_type": p.option_type, "strike": p.strike,
        "expiration": p.expiration, "delta_at_entry": p.delta_at_entry,
        "contracts": p.contracts, "entry_price": p.entry_price,
        "tp_price": tp_price, "sl_price": sl_price,
        "exits_provisional": provisional,
        "limit_price": _limit_price(p) if p.entry_price is None else None,
        "tp_pct": p.tp_pct, "sl_pct": p.sl_pct, "buy_pct": p.buy_pct,
        "exit_price": p.exit_price, "pnl_usd": p.pnl_usd,
        "note": p.note,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
    }
