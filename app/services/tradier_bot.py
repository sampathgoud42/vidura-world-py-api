"""Tradier options executor: pick by delta, size by balance %, buy, then
manage the exit — resting TP sell on the venue, monitored SL that cancels
the TP before selling.

The trade the desk asked for (user 08/03):

    balance $500 · bid $0.50 · buy 50%  ->  $250 / ($0.50 x 100) = 5 contracts
    tp 15%  ->  resting sell at entry x 1.15
    sl 30%  ->  when the bid touches entry x 0.70, cancel the TP and sell

The x100 contract multiplier is the part the shorthand arithmetic usually
skips — sizing that forgot it would order 100x too many contracts.

Multiple positions run concurrently; each is a row in tradier_positions and
the monitor sweeps them all. The TP order survives an API restart because it
rests on the venue; the SL survives because the rows do.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import User
from app.models.tradier import TradierPosition
from app.services import credentials
from app.services.tradier_client import TradierClient, TradierError

logger = logging.getLogger(__name__)

# Desk-wide defaults live in Settings (env: VIDURA_TRADIER_*); these module
# aliases keep every existing call-site and API Field(default=...) working.
_S = get_settings()
DEFAULT_DELTA_MIN = _S.tradier_delta_min
DEFAULT_DELTA_MAX = _S.tradier_delta_max
DEFAULT_BUY_PCT = _S.tradier_buy_pct
DEFAULT_TP_PCT = _S.tradier_tp_pct
DEFAULT_SL_PCT = _S.tradier_sl_pct

ACTIVE_STATUSES = ("pending", "open")


class TradierBotError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def client_for(user: User, *, live: bool = False) -> TradierClient:
    """Build a client for this user. SANDBOX unless LIVE is asked for.

    The default is deliberately the paper venue: every desk call that does not
    name an environment (quotes, chain preview, balance) lands on sandbox, so
    reaching the real account is always a deliberate act.

    VIDURA_PAPER_ONLY=true refuses live outright rather than silently
    downgrading it — the same contract the Kalshi bots use, and an operator
    who asked for live money deserves to be told the server is locked.
    """
    if live and get_settings().paper_only:
        raise TradierBotError(
            "server is locked to paper (VIDURA_PAPER_ONLY=true) — live Tradier "
            "orders are refused; unset it to trade the production account", 403
        )
    creds = credentials.load_tradier_credentials(
        user.user_root_folder, sandbox=not live
    )
    return TradierClient(creds)


# ── selection & sizing ──────────────────────────────────────────────────────

def pick_contract(chain: list[dict], side: str, delta_min: float,
                  delta_max: float) -> dict | None:
    """The option whose |delta| sits closest to the band's middle.

    |delta| everywhere: puts carry negative delta, and filtering on the raw
    value would find no puts in a 0.25..0.50 band. Requires a live two-sided
    quote — an option with no bid cannot be exited, so it must never be
    entered. Ties break toward the tighter spread.
    """
    mid_target = (delta_min + delta_max) / 2.0
    best = None
    for opt in chain:
        if (opt.get("option_type") or "").lower() != side:
            continue
        greeks = opt.get("greeks") or {}
        delta = greeks.get("delta")
        if delta is None:
            continue
        d = abs(float(delta))
        if not (delta_min <= d <= delta_max):
            continue
        bid = float(opt.get("bid") or 0)
        ask = float(opt.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        score = (abs(d - mid_target), ask - bid)
        if best is None or score < best[0]:
            best = (score, opt, d)
    if best is None:
        return None
    opt = dict(best[1])
    opt["_abs_delta"] = best[2]
    return opt


def size_contracts(balance_usd: float, buy_pct: float, limit_price: float) -> int:
    """floor((balance x pct) / (price x 100)) — the x100 is the option
    multiplier the desk's shorthand omits; without it this would order a
    hundred times the intended size."""
    if limit_price <= 0:
        return 0
    budget = balance_usd * (buy_pct / 100.0)
    return max(0, math.floor(budget / (limit_price * 100.0)))


# ── open ────────────────────────────────────────────────────────────────────

def open_position(
    db: Session,
    user: User,
    *,
    symbol: str,
    side: str,
    buy_pct: float = DEFAULT_BUY_PCT,
    delta_min: float = DEFAULT_DELTA_MIN,
    delta_max: float = DEFAULT_DELTA_MAX,
    tp_pct: float = DEFAULT_TP_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
    expiration: str | None = None,
    min_contracts: int = 1,
    strategy: str = "Manual",
    live: bool = False,
) -> TradierPosition:
    """Select, size and buy; the monitor takes over from there.

    ``live=False`` (the default) places the order on the SANDBOX venue with
    the sandbox token — a mock order against fake money.
    """
    symbol = symbol.strip().upper()
    side = side.strip().lower()
    if side not in ("call", "put"):
        raise TradierBotError("side must be 'call' or 'put'")
    if not (0 < buy_pct <= 100):
        raise TradierBotError("buy_pct must be in (0, 100]")
    if not (0 < delta_min < delta_max <= 1):
        raise TradierBotError("need 0 < delta_min < delta_max <= 1")

    client = client_for(user, live=live)
    venue_sandbox = client.creds.sandbox
    try:
        bal = client.balances()
        buying_power = bal["option_buying_power"]
        if buying_power <= 0:
            raise TradierBotError(
                f"option buying power is ${buying_power:.2f} — nothing to size against", 409
            )

        if expiration is None:
            exps = client.expirations(symbol)
            if not exps:
                raise TradierBotError(f"no option expirations for {symbol}", 404)
            expiration = exps[0]        # nearest listed expiration

        chain = client.chain(symbol, expiration)
        opt = pick_contract(chain, side, delta_min, delta_max)
        if opt is None:
            raise TradierBotError(
                f"no {side} on {symbol} {expiration} with |delta| in "
                f"{delta_min:g}-{delta_max:g} and a two-sided quote", 404
            )

        limit_price = float(opt["ask"])      # what a buy actually costs
        contracts = size_contracts(buying_power, buy_pct, limit_price)
        if contracts < max(1, min_contracts):
            raise TradierBotError(
                f"sized {contracts} contract(s) — below min_contracts "
                f"{max(1, min_contracts)}: {buy_pct:g}% of ${buying_power:.2f} "
                f"at ${limit_price:.2f} (${limit_price * 100:.2f}/contract) "
                f"for {opt['symbol']}", 409
            )

        order = client.place_option_order(
            underlying=symbol, occ_symbol=opt["symbol"], side="buy_to_open",
            quantity=contracts, order_type="limit", price=limit_price,
        )
    finally:
        client.close()

    pos = TradierPosition(
        user_id=user.user_id,
        # The venue the order ACTUALLY went to — the monitor and the manual
        # close read this back to reach the right account.
        sandbox=venue_sandbox,
        underlying=symbol,
        occ_symbol=opt["symbol"],
        option_type=side,
        strike=float(opt.get("strike") or 0),
        expiration=expiration,
        delta_at_entry=opt.get("_abs_delta"),
        contracts=contracts,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        buy_pct=buy_pct,
        buy_order_id=str(order["id"]),
        strategy=(strategy or "Manual")[:64],
        status="pending",
        note=f"buy_to_open {contracts} @ {limit_price:.2f} limit",
        raw={"buy_order": order, "picked": {
            "bid": opt.get("bid"), "ask": opt.get("ask"),
            "delta": opt.get("_abs_delta"),
        }},
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    logger.info("tradier: opened #%s %s %s x%s (delta %.2f)",
                pos.id, pos.occ_symbol, side, contracts, pos.delta_at_entry or 0)
    return pos


# ── monitor ─────────────────────────────────────────────────────────────────

def exit_prices(entry: float, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    """(take-profit, stop) for an entry price.

    Ceil to the penny, not round(): round(0.575, 2) is 0.57 under banker's
    rounding — a TP that sells BELOW the promised percent. Ceiling errs
    protective on both sides: the TP never sells under its target, the SL
    never stops later than its floor.
    """
    def _ceil_penny(x: float) -> float:
        return math.ceil(x * 100 - 1e-6) / 100.0

    return (_ceil_penny(entry * (1 + tp_pct / 100.0)),
            _ceil_penny(entry * (1 - sl_pct / 100.0)))


def _target_override(pos: TradierPosition) -> float | None:
    """An explicit target the operator set, which outranks the percentage."""
    raw = pos.raw if isinstance(pos.raw, dict) else {}
    try:
        px = float((raw.get("tp_override") or {}).get("price"))
        return px if px > 0 else None
    except (TypeError, ValueError):
        return None


def _place_tp(client: TradierClient, pos: TradierPosition) -> None:
    """Entry filled -> arm the exits. TP rests on the venue (survives us);
    the SL threshold is stored and watched by the sweep."""
    entry = pos.entry_price or 0
    pos.tp_price, pos.sl_price = exit_prices(entry, pos.tp_pct, pos.sl_pct)
    # A target set before the fill wins over the percentage — the operator
    # named a price, and the percentage was only ever a way to reach one.
    override = _target_override(pos)
    if override:
        pos.tp_price = override
        if entry:
            pos.tp_pct = round((override / entry - 1) * 100, 2)
    tp = client.place_option_order(
        underlying=pos.underlying, occ_symbol=pos.occ_symbol,
        side="sell_to_close", quantity=pos.contracts,
        order_type="limit", price=pos.tp_price, duration="gtc",
    )
    pos.tp_order_id = str(tp["id"])
    pos.status = "open"
    pos.note = (f"filled @ {entry:.2f}; TP {pos.tp_price:.2f} resting, "
                f"SL {pos.sl_price:.2f} monitored")


def _finalize(pos: TradierPosition, status: str, exit_price: float | None,
              note: str) -> None:
    pos.status = status
    pos.exit_price = exit_price
    if exit_price is not None and pos.entry_price is not None:
        pos.pnl_usd = round((exit_price - pos.entry_price) * 100 * pos.contracts, 2)
    pos.note = note
    pos.closed_at = _now()


def monitor_pass(db: Session, user: User) -> dict:
    """One sweep over this user's active positions. Called by the API's
    background loop and by the desk's refresh — safe to run concurrently
    with itself only in the sense that every action re-checks venue state
    first (fills are detected from order status, not assumed)."""
    rows = list(db.scalars(
        select(TradierPosition).where(
            TradierPosition.user_id == user.user_id,
            TradierPosition.status.in_(ACTIVE_STATUSES),
        )
    ).all())
    if not rows:
        return {"checked": 0, "events": []}

    # One client per VENUE, chosen by each row's own sandbox flag. Sweeping a
    # live position against sandbox would find no such order id and finalize
    # a real, still-open position as failed.
    events: list[str] = []
    clients: dict[bool, TradierClient] = {}
    try:
        for pos in rows:
            want_live = not pos.sandbox
            try:
                client = clients.get(want_live)
                if client is None:
                    client = clients[want_live] = client_for(user, live=want_live)
                _monitor_one(client, pos, events)
            except TradierError as exc:
                # transient venue trouble must not kill the sweep for the
                # other positions; this row is retried next pass
                events.append(f"#{pos.id}: venue error, retrying ({exc})")
            except (TradierBotError, credentials.CredentialsError) as exc:
                # e.g. a live row while the server is paper-locked: leave it
                # untouched rather than mislabel it from the wrong venue
                events.append(f"#{pos.id}: skipped ({exc})")
        db.commit()
    finally:
        for client in clients.values():
            client.close()
    return {"checked": len(rows), "events": events}


def _monitor_one(client: TradierClient, pos: TradierPosition,
                 events: list[str]) -> None:
    if pos.status == "pending":
        order = client.order_status(pos.buy_order_id)
        st = (order.get("status") or "").lower()
        if st == "filled":
            pos.entry_price = float(order.get("avg_fill_price") or 0)
            _place_tp(client, pos)
            events.append(f"#{pos.id} filled @ {pos.entry_price:.2f}; exits armed")
        elif st in ("canceled", "rejected", "expired"):
            _finalize(pos, "failed", None, f"buy {st}; nothing at risk")
            events.append(f"#{pos.id} buy {st}")
        return

    # open: TP filled? then SL?
    if pos.tp_order_id:
        tp = client.order_status(pos.tp_order_id)
        st = (tp.get("status") or "").lower()
        if st == "filled":
            _finalize(pos, "tp_filled",
                      float(tp.get("avg_fill_price") or pos.tp_price or 0),
                      "TP filled")
            events.append(f"#{pos.id} TP filled @ {pos.exit_price:.2f}")
            return
        if st in ("canceled", "rejected", "expired"):
            # someone cancelled our TP out from under us — re-arm it rather
            # than leave the position with no exit order at all
            _place_tp(client, pos)
            events.append(f"#{pos.id} TP was {st}; re-armed")
            return

    quote = client.quote(pos.occ_symbol)
    bid = float(quote.get("bid") or 0)
    if pos.sl_price is not None and 0 < bid <= pos.sl_price:
        # ORDER MATTERS: cancel the TP first. Selling while the TP still
        # rests would stack two sells for one position and the second fill
        # would open a naked short.
        if pos.tp_order_id:
            try:
                client.cancel_order(pos.tp_order_id)
            except TradierError as exc:
                # if the TP filled in this exact window, that IS the exit
                tp = client.order_status(pos.tp_order_id)
                if (tp.get("status") or "").lower() == "filled":
                    _finalize(pos, "tp_filled",
                              float(tp.get("avg_fill_price") or 0),
                              "TP filled during SL race")
                    events.append(f"#{pos.id} TP won the race")
                    return
                raise exc
        sell = client.place_option_order(
            underlying=pos.underlying, occ_symbol=pos.occ_symbol,
            side="sell_to_close", quantity=pos.contracts,
            order_type="market",
        )
        pos.close_order_id = str(sell["id"])
        _finalize(pos, "sl_sold", bid,
                  f"SL: bid {bid:.2f} <= {pos.sl_price:.2f}; TP cancelled, sold")
        events.append(f"#{pos.id} SL hit @ {bid:.2f}")


def live_quotes(user: User, rows) -> dict[str, dict]:
    """``{occ_symbol: {bid, ask, last}}`` for the still-active rows.

    One batch request per venue, and none at all when nothing is active —
    the desk polls this every few seconds and Tradier rate-limits per token.
    Quote trouble is swallowed: a stale mark must never break the table.
    """
    by_venue: dict[bool, set[str]] = {}
    for p in rows:
        if p.status in ACTIVE_STATUSES and p.occ_symbol:
            by_venue.setdefault(not p.sandbox, set()).add(p.occ_symbol)
    out: dict[str, dict] = {}
    for want_live, syms in by_venue.items():
        try:
            client = client_for(user, live=want_live)
        except Exception:                         # noqa: BLE001 - venue not usable
            continue
        try:
            for q in client.quotes(sorted(syms)):
                sym = q.get("symbol")
                if sym:
                    out[sym] = {"bid": q.get("bid"), "ask": q.get("ask"),
                                "last": q.get("last")}
        except TradierError:
            pass
        finally:
            client.close()
    return out


def set_target(db: Session, user: User, position_id: int,
               target_price: float) -> TradierPosition:
    """Move a live position's take-profit to an explicit price.

    For an OPEN position the resting sell is replaced on the venue, so the
    exit still happens without us. The dangerous part is the swap: if the old
    order filled between our cancel and our replace we would be selling a
    position we no longer hold, so the old order's status is re-read after
    the cancel and a filled one aborts the edit — the monitor then finalizes
    it normally.

    For a PENDING position there is no resting order yet; the target is
    recorded and armed at fill instead.
    """
    pos = db.get(TradierPosition, position_id)
    if pos is None or pos.user_id != user.user_id:
        raise TradierBotError(f"position {position_id} not found", 404)
    if pos.status not in ACTIVE_STATUSES:
        raise TradierBotError(
            f"position {position_id} is {pos.status} — only a live position "
            "has a target to move", 409)
    try:
        target = round(float(target_price), 2)
    except (TypeError, ValueError) as exc:
        raise TradierBotError("target must be a price") from exc
    if target <= 0:
        raise TradierBotError("target must be greater than zero")
    if pos.sl_price and target <= pos.sl_price:
        raise TradierBotError(
            f"target {target:.2f} is at or below the stop {pos.sl_price:.2f} — "
            "the position would be stopped out before it could ever reach it", 409)

    client = client_for(user, live=not pos.sandbox)
    try:
        if pos.status == "open" and pos.tp_order_id:
            try:
                client.cancel_order(pos.tp_order_id)
            except TradierError:
                pass                      # already gone; the status check rules
            st = (client.order_status(pos.tp_order_id).get("status") or "").lower()
            if st == "filled":
                raise TradierBotError(
                    "the take-profit filled while the target was being moved — "
                    "the position is already closing", 409)
            order = client.place_option_order(
                underlying=pos.underlying, occ_symbol=pos.occ_symbol,
                side="sell_to_close", quantity=pos.contracts,
                order_type="limit", price=target, duration="gtc",
            )
            pos.tp_order_id = str(order["id"])
            pos.note = (f"target moved to {target:.2f}"
                        + (f" (entry {pos.entry_price:.2f})" if pos.entry_price else ""))
        else:
            pos.note = f"target {target:.2f} set; arms when the buy fills"
    finally:
        client.close()

    pos.tp_price = target
    if pos.entry_price:
        pos.tp_pct = round((target / pos.entry_price - 1) * 100, 2)
    raw = dict(pos.raw or {})
    raw["tp_override"] = {"price": target, "at": _now().isoformat()}
    pos.raw = raw
    db.commit()
    db.refresh(pos)
    return pos


def close_position(db: Session, user: User, position_id: int,
                   force: bool = False) -> TradierPosition:
    """Manual close from the desk: cancel whatever rests, sell at market.

    ``force`` only applies to a PENDING row on the SANDBOX whose cancel the
    venue refuses: the row stops being tracked so the desk can be cleared.
    It is deliberately not available on the live account — abandoning a real
    working order would leave a position nobody is watching.
    """
    pos = db.get(TradierPosition, position_id)
    if pos is None or pos.user_id != user.user_id:
        raise TradierBotError(f"position {position_id} not found", 404)
    if pos.status not in ACTIVE_STATUSES:
        raise TradierBotError(f"position {position_id} is {pos.status}", 409)

    # Close on the venue the position was opened on, never the desk's default.
    client = client_for(user, live=not pos.sandbox)
    try:
        if pos.status == "pending" and pos.buy_order_id:
            try:
                client.cancel_order(pos.buy_order_id)
            except TradierError as exc:
                # A cancel can fail because the order is already gone — or
                # because the venue itself is unwell (Tradier's sandbox 500s
                # on DELETE while happily serving the same order on GET).
                # Ask what the order actually IS before deciding.
                try:
                    st = (client.order_status(pos.buy_order_id).get("status")
                          or "").lower()
                except TradierError:
                    st = ""
                if st in ("canceled", "cancelled", "expired", "rejected"):
                    pass                  # already dead; fall through and close
                elif st == "filled":
                    raise TradierBotError(
                        "the buy filled while it was being cancelled — refresh "
                        "and close the position instead", 409) from exc
                elif force and pos.sandbox:
                    _finalize(pos, "failed", None,
                              f"stopped tracking: venue would not cancel "
                              f"({str(exc)[:80]}); order may still rest on the "
                              f"sandbox")
                    db.commit()
                    return pos
                else:
                    raise TradierBotError(
                        f"the venue refused to cancel this order and it is "
                        f"still {st or 'working'} — the position is untouched. "
                        f"({exc})", 502) from exc
            _finalize(pos, "failed", None, "buy cancelled from the desk")
        else:
            if pos.tp_order_id:
                try:
                    client.cancel_order(pos.tp_order_id)
                except TradierError:
                    pass                      # already gone is fine here
            quote = client.quote(pos.occ_symbol)
            sell = client.place_option_order(
                underlying=pos.underlying, occ_symbol=pos.occ_symbol,
                side="sell_to_close", quantity=pos.contracts,
                order_type="market",
            )
            pos.close_order_id = str(sell["id"])
            _finalize(pos, "closed", float(quote.get("bid") or 0) or None,
                      "closed from the desk")
        db.commit()
        db.refresh(pos)
    finally:
        client.close()
    return pos
