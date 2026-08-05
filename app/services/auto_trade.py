"""Auto-trade: the opening-range level-cross strategy, one watcher per user.

Armed by the desk's AUTO TRADE button (which opens a form: strategy,
tickers, CST time window, buy/tp/sl %, min contracts). Every poll it reads
the level-cross snapshot (levels_watcher.py via services.levels) and, for
the configured tickers:

  * a NEW ``above_10min_high`` cross  ->  CALL candidate
  * a NEW ``below_10min_low``  cross  ->  PUT  candidate

Only crosses stamped inside the window (default 08:30-09:30 CST — the
opening-range play; the range itself is marked 08:30-08:40, crosses start
08:45) qualify. A candidate is NOT traded immediately: it waits confirm_s
(default 5 minutes), then the snapshot is read again and the trade fires
only if that level's latest signal is STILL the same — a flip back inside
the range cancels the candidate.

The order is the desk's normal managed position (tradier_bot.open_position)
pinned to TODAY's expiration (0DTE), sized by % of option buying power. If
that sizing lands below min_contracts the trade is SKIPPED (recorded, not
retried). TP rests on the venue and the SL is watched by the API's monitor
loop — exits need nothing from this module.

Defaults for every knob live in Settings under env prefix VIDURA_TRADIER_*
(tradier_auto_strategy, tradier_auto_tickers, tradier_auto_window_open/
close, tradier_auto_confirm_s, tradier_auto_poll_s,
tradier_auto_min_contracts, and the desk-wide buy/tp/sl/delta defaults).

The watcher survives across days: each CST date it re-arms for the window
and trades each (date, ticker, level, bar) instance at most once. It runs
until stopped from the desk (or the API restarts — state is in-process by
design; re-arm is one click).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CST = ZoneInfo("America/Chicago")

STRATEGIES = ("10min_intraday_move",)
SIGNAL_SIDE = {"above_10min_high": "call", "below_10min_low": "put"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_WATCHERS: dict[str, dict] = {}      # user_id -> watcher record
_LOCK = threading.Lock()


class AutoTradeError(RuntimeError):
    pass


def _now_cst() -> datetime:
    return datetime.now(CST)


def _log_event(w: dict, msg: str) -> None:
    w["events"].insert(0, f"{_now_cst():%H:%M:%S} {msg}")
    del w["events"][40:]
    logger.info("auto-trade[%s]: %s", w["user_id"][:8], msg)


def _crosses(snapshot: dict | None, tickers: list[str]) -> list[dict]:
    """The tradeable crosses in a levels snapshot: one per ticker+level."""
    out = []
    for tkr in tickers:
        latest = ((snapshot or {}).get("tickers", {}).get(tkr) or {}).get("latest") or {}
        for level, s in latest.items():
            sig = (s or {}).get("signal")
            if sig in SIGNAL_SIDE:
                out.append({"ticker": tkr, "level": level, "signal": sig,
                            "time": (s or {}).get("time") or ""})
    return out


def _place(w: dict, cand: dict) -> None:
    """Confirmed candidate -> the desk's normal managed 0DTE position."""
    from app.core.database import SessionLocal
    from app.models import User
    from app.services import tradier_bot

    p = w["params"]
    side = SIGNAL_SIDE[cand["signal"]]
    expiration = f"{_now_cst():%Y-%m-%d}"          # 0DTE: today's expiry
    db = SessionLocal()
    try:
        user = db.get(User, w["user_id"])
        if user is None:
            raise AutoTradeError("user vanished")
        pos = tradier_bot.open_position(
            db, user,
            symbol=cand["ticker"], side=side,
            buy_pct=p["buy_pct"],
            delta_min=p["delta_min"], delta_max=p["delta_max"],
            tp_pct=p["tp_pct"], sl_pct=p["sl_pct"],
            expiration=expiration,
            min_contracts=p["min_contracts"],
            strategy=p["strategy"],
        )
        w["attempts"].append({
            "at": f"{_now_cst():%m-%d %H:%M:%S}", "ticker": cand["ticker"],
            "signal": cand["signal"], "side": side, "status": "placed",
            "position_id": pos.id, "occ_symbol": pos.occ_symbol,
            "contracts": pos.contracts,
        })
        _log_event(w, f"PLACED {cand['ticker']} {side.upper()} x{pos.contracts} "
                      f"({pos.occ_symbol}) from {cand['signal']}")
    except tradier_bot.TradierBotError as exc:
        skipped = "min_contracts" in str(exc)
        w["attempts"].append({
            "at": f"{_now_cst():%m-%d %H:%M:%S}", "ticker": cand["ticker"],
            "signal": cand["signal"], "side": side,
            "status": "skipped" if skipped else "failed",
            "error": str(exc)[:300],
        })
        _log_event(w, f"{'SKIPPED' if skipped else 'FAILED'} "
                      f"{cand['ticker']} {side.upper()}: {exc}")
    except Exception as exc:  # noqa: BLE001 — one bad order must not kill the watcher
        w["attempts"].append({
            "at": f"{_now_cst():%m-%d %H:%M:%S}", "ticker": cand["ticker"],
            "signal": cand["signal"], "side": side, "status": "failed",
            "error": str(exc)[:300],
        })
        _log_event(w, f"FAILED {cand['ticker']} {side.upper()}: {exc}")
    finally:
        db.close()


def _run(w: dict) -> None:
    from app.services import levels as levels_svc

    p = w["params"]
    baseline_day = None
    while not w["stop"].is_set():
        try:
            now = _now_cst()
            today = f"{now:%Y-%m-%d}"
            hhmm = f"{now:%H:%M}"

            snap = levels_svc.status().get("status")
            crosses = _crosses(snap, p["tickers"])

            # a new CST day: whatever is in the snapshot predates today's
            # window, so it is baseline, not signal
            if baseline_day != today:
                baseline_day = today
                w["seen"] = {(today, c["ticker"], c["level"], c["time"])
                             for c in crosses}
                w["pending"] = []
                _log_event(w, f"armed for {today} · window "
                              f"{p['window_open']}-{p['window_close']} CST")

            # detect NEW crosses inside the window
            for c in crosses:
                key = (today, c["ticker"], c["level"], c["time"])
                if key in w["seen"]:
                    continue
                w["seen"].add(key)
                if (not (p["window_open"] <= c["time"] < p["window_close"])
                        or hhmm >= p["window_close"]):
                    _log_event(w, f"ignored {c['ticker']} {c['signal']} @ {c['time']} "
                                  f"(outside {p['window_open']}-{p['window_close']})")
                    continue
                confirm_at = time.monotonic() + p["confirm_s"]
                w["pending"].append({**c, "confirm_at": confirm_at,
                                     "confirm_clock": f"{now + timedelta(seconds=p['confirm_s']):%H:%M:%S}"})
                _log_event(w, f"candidate {c['ticker']} {c['signal']} @ {c['time']} "
                              f"— confirming at +{p['confirm_s'] // 60}m")

            # confirm due candidates against the CURRENT snapshot
            due = [x for x in w["pending"] if time.monotonic() >= x["confirm_at"]]
            for cand in due:
                w["pending"].remove(cand)
                latest = ((snap or {}).get("tickers", {}).get(cand["ticker"]) or {}
                          ).get("latest") or {}
                still = (latest.get(cand["level"]) or {}).get("signal") == cand["signal"]
                if still:
                    _place(w, cand)
                else:
                    _log_event(w, f"dropped {cand['ticker']} {cand['signal']} — signal "
                                  "changed during confirmation")
        except Exception as exc:  # noqa: BLE001
            _log_event(w, f"watcher error: {exc}")
        w["stop"].wait(p["poll_s"])
    _log_event(w, "stopped")


def start(user_id: str, *, buy_pct: float | None = None,
          tp_pct: float | None = None, sl_pct: float | None = None,
          delta_min: float | None = None, delta_max: float | None = None,
          strategy: str | None = None, tickers: str | list[str] | None = None,
          window_open: str | None = None, window_close: str | None = None,
          min_contracts: int | None = None) -> dict:
    s = get_settings()
    params = {
        "strategy": (strategy or s.tradier_auto_strategy).strip(),
        "buy_pct": s.tradier_buy_pct if buy_pct is None else buy_pct,
        "tp_pct": s.tradier_tp_pct if tp_pct is None else tp_pct,
        "sl_pct": s.tradier_sl_pct if sl_pct is None else sl_pct,
        "delta_min": s.tradier_delta_min if delta_min is None else delta_min,
        "delta_max": s.tradier_delta_max if delta_max is None else delta_max,
        "window_open": (window_open or s.tradier_auto_window_open).strip(),
        "window_close": (window_close or s.tradier_auto_window_close).strip(),
        "min_contracts": (s.tradier_auto_min_contracts
                          if min_contracts is None else int(min_contracts)),
        "confirm_s": s.tradier_auto_confirm_s,
        "poll_s": max(5, s.tradier_auto_poll_s),
    }
    raw = tickers if tickers is not None else s.tradier_auto_tickers
    if isinstance(raw, str):
        raw = raw.split(",")
    params["tickers"] = [t.strip().upper() for t in raw if t.strip()]

    if params["strategy"] not in STRATEGIES:
        raise AutoTradeError(f"unknown strategy '{params['strategy']}' "
                             f"(supported: {', '.join(STRATEGIES)})")
    if not params["tickers"]:
        raise AutoTradeError("at least one ticker is required")
    if any(not re.match(r"^[A-Z0-9^.\-]{1,10}$", t) for t in params["tickers"]):
        raise AutoTradeError("tickers must be plain symbols like SPY, QQQ, SPX")
    for k in ("window_open", "window_close"):
        if not _HHMM.match(params[k]):
            raise AutoTradeError(f"{k} must be HH:MM (24h CST)")
    if params["window_open"] >= params["window_close"]:
        raise AutoTradeError("window_open must be before window_close")
    if not (0 < params["buy_pct"] <= 100):
        raise AutoTradeError("buy_pct must be in (0, 100]")
    if not (0 < params["delta_min"] < params["delta_max"] <= 1):
        raise AutoTradeError("need 0 < delta_min < delta_max <= 1")
    if params["min_contracts"] < 1:
        raise AutoTradeError("min_contracts must be >= 1")

    with _LOCK:
        existing = _WATCHERS.get(user_id)
        if existing and existing["thread"].is_alive() and not existing["stop"].is_set():
            existing["params"] = params
            _log_event(existing, "params updated (already armed)")
            return status(user_id)
        w = {
            "user_id": user_id,
            "params": params,
            "stop": threading.Event(),
            "seen": set(), "pending": [], "attempts": [], "events": [],
            "started_at": f"{_now_cst():%m-%d %H:%M:%S}",
        }
        w["thread"] = threading.Thread(target=_run, args=(w,), daemon=True,
                                       name=f"autotrade-{user_id[:8]}")
        _WATCHERS[user_id] = w
        w["thread"].start()
    return status(user_id)


def stop(user_id: str) -> dict:
    with _LOCK:
        w = _WATCHERS.get(user_id)
        if w:
            w["stop"].set()
    return status(user_id)


def defaults() -> dict:
    """What the arm form should prefill (Settings, env VIDURA_TRADIER_*)."""
    s = get_settings()
    return {
        "strategy": s.tradier_auto_strategy,
        "strategies": list(STRATEGIES),
        "tickers": s.tradier_auto_tickers,
        "window_open": s.tradier_auto_window_open,
        "window_close": s.tradier_auto_window_close,
        "buy_pct": s.tradier_buy_pct,
        "tp_pct": s.tradier_tp_pct,
        "sl_pct": s.tradier_sl_pct,
        "min_contracts": s.tradier_auto_min_contracts,
        "confirm_s": s.tradier_auto_confirm_s,
    }


def status(user_id: str) -> dict:
    w = _WATCHERS.get(user_id)
    if w is None:
        return {"active": False, "defaults": defaults()}
    p = w["params"]
    return {
        "active": w["thread"].is_alive() and not w["stop"].is_set(),
        "started_at": w["started_at"],
        "params": {k: v for k, v in p.items()},
        "strategy": p["strategy"],
        "window": f"{p['window_open']}-{p['window_close']} CST",
        "tickers": p["tickers"],
        "pending": [{k: x[k] for k in ("ticker", "signal", "time", "confirm_clock")}
                    for x in w["pending"]],
        "attempts": w["attempts"][-20:],
        "events": w["events"][:12],
        "paper": get_settings().paper_only,
        "defaults": defaults(),
    }
