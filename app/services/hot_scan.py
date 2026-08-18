"""HOT: which of the large caps are in a strong, one-sided uptrend right now.

The desk question this answers: of the top-100 names, which ones is the
directional-movement system saying are TRENDING UP hard enough to be worth a
call — not merely drifting up, and not chopping.

Three gates, all on Wilder's 14-period DMI/ADX (user 08/17):

    +DI  >  25          the up-move is substantial in its own right
    +DI >= -DI x 2      and it dominates the down-move, not just leads it
    ADX  >  34          and the whole thing is a trend, not a range

The middle one is the interesting gate. +DI above -DI is a crossover, which
happens constantly and reverses just as often; TWICE -DI is a different claim
— that the buyers are not being answered. ADX then says the market agrees:
above 34 is a strong trend by Wilder's own reading, well past the usual 20-25
"trend exists" line.

The arithmetic is a port of the desk chart's adxCompute, deliberately line for
line. If the two drifted, a name could be HOT here and show no B marker on its
own chart, which is worse than having no scanner at all.

Cost is why this caches: one timesales call per symbol, so a 100-name sweep is
100 calls. A request never waits for it — the last good snapshot comes back
immediately and a stale one refreshes in the background.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CST = ZoneInfo("America/Chicago")

# user_id -> {"at": monotonic, "day": ..., "rows": [...], "meta": {...}}
_CACHE: dict[str, dict] = {}
_REFRESHING: set[str] = set()
_LOCK = threading.Lock()


def _today() -> str:
    return f"{datetime.now(CST):%Y-%m-%d}"


def universe() -> list[str]:
    s = get_settings()
    return [t.strip().upper() for t in s.tradier_hot_universe.split(",") if t.strip()]


# ── Wilder DMI/ADX ──────────────────────────────────────────────────────────

def _wilder(src: list[float], period: int) -> list[float | None]:
    """Seed with a sum over the first `period`, then decay by 1/period.

    Not a moving average of the last `period` values — Wilder's own smoothing,
    which is what every charting package draws and therefore the only version
    whose numbers match the ones on screen.
    """
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(src):
        if i < period:
            acc += v
            out.append(acc if i == period - 1 else None)
        else:
            acc = acc - acc / period + v
            out.append(acc)
    return out


def dmi(bars: list[dict], period: int = 14) -> dict | None:
    """Latest {plus_di, minus_di, adx} for these bars, or None if too few.

    ``bars`` are dicts with high/low/close. Needs 2*period+2 of them: the ADX
    is an average of DX values that are themselves smoothed, so a short series
    produces a number that is mostly its own seed.
    """
    n = len(bars)
    if n < period * 2 + 2:
        return None

    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        h, low = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        ph, pl = float(bars[i - 1]["high"]), float(bars[i - 1]["low"])
        tr.append(max(h - low, abs(h - pc), abs(low - pc)))
        up = h - ph
        down = pl - low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    s_tr = _wilder(tr, period)
    s_p = _wilder(plus_dm, period)
    s_m = _wilder(minus_dm, period)

    plus_di: list[float | None] = []
    minus_di: list[float | None] = []
    dx: list[float | None] = []
    for i in range(len(tr)):
        if s_tr[i] is None or s_tr[i] == 0:
            plus_di.append(None)
            minus_di.append(None)
            dx.append(None)
            continue
        pdi = 100.0 * (s_p[i] / s_tr[i])
        mdi = 100.0 * (s_m[i] / s_tr[i])
        plus_di.append(pdi)
        minus_di.append(mdi)
        total = pdi + mdi
        dx.append(0.0 if total == 0 else 100.0 * (abs(pdi - mdi) / total))

    first = next((i for i, v in enumerate(dx) if v is not None), None)
    if first is None or first + period > len(dx):
        return None
    adx: list[float | None] = [None] * len(dx)
    adx[first + period - 1] = sum(dx[first:first + period]) / period
    for i in range(first + period, len(dx)):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    if plus_di[-1] is None or minus_di[-1] is None or adx[-1] is None:
        return None
    return {"plus_di": plus_di[-1], "minus_di": minus_di[-1], "adx": adx[-1]}


def is_hot(reading: dict, s=None) -> bool:
    """The three gates, in the order they are spoken."""
    s = s or get_settings()
    pdi, mdi, adx = reading["plus_di"], reading["minus_di"], reading["adx"]
    return (pdi > s.tradier_hot_min_pdi
            and pdi >= mdi * s.tradier_hot_di_ratio
            and adx > s.tradier_hot_min_adx)


# ── the sweep ───────────────────────────────────────────────────────────────

def _regular_session(rows: list[dict]) -> list[dict]:
    """09:30-16:00 only — the same slice the desk chart draws.

    Extended-hours bars are thin and gappy, and a gap is a large true range,
    so leaving them in inflates ADX. The chart drops them; if the scanner did
    not, a name could rank HOT here on overnight noise and show no marker on
    its own chart.
    """
    out = []
    for r in rows:
        hm = str(r.get("time") or "")[11:16]
        if "09:30" <= hm <= "16:00":
            out.append(r)
    return out


def _bars(client, symbol: str, interval: str, days: int) -> list[dict]:
    start = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    rows = client.timesales(symbol, interval, start=start)
    rows = [r for r in rows
            if r.get("high") is not None and r.get("low") is not None
            and r.get("close") is not None]
    return _regular_session(rows)


def _scan_symbol(client, symbol: str, interval: str, days: int) -> dict | None:
    try:
        bars = _bars(client, symbol, interval, days)
    except Exception as exc:                              # noqa: BLE001
        logger.debug("hot scan %s: %s", symbol, exc)
        return None
    reading = dmi(bars)
    if reading is None:
        return None
    last = bars[-1]
    return {
        "symbol": symbol,
        "plus_di": round(reading["plus_di"], 2),
        "minus_di": round(reading["minus_di"], 2),
        "adx": round(reading["adx"], 2),
        # how far past the ratio gate it is — 2.0 is the threshold itself, so
        # this ranks "buyers unanswered" rather than ranking raw +DI
        "di_ratio": round(reading["plus_di"] / reading["minus_di"], 2)
        if reading["minus_di"] > 0 else None,
        "last": last.get("close"),
        "bars": len(bars),
    }


def _refresh(user, live: bool, interval: str) -> dict:
    from app.services.tradier_bot import client_for

    s = get_settings()
    syms = universe()
    started = time.time()
    client = client_for(user, live=live)
    rows: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=s.tradier_flow_workers) as pool:
            for got in pool.map(
                lambda sym: _scan_symbol(client, sym, interval, s.tradier_hot_days),
                syms,
            ):
                if got is not None:
                    rows.append(got)
    finally:
        client.close()

    hot = [r for r in rows if is_hot({"plus_di": r["plus_di"],
                                      "minus_di": r["minus_di"],
                                      "adx": r["adx"]}, s)]
    # strongest trend first; the ratio breaks ties, since two names at ADX 40
    # are not equally one-sided
    hot.sort(key=lambda r: (r["adx"], r["di_ratio"] or 0), reverse=True)
    return {
        "rows": hot,
        "meta": {
            "day": _today(),
            "interval": interval,
            "scanned": len(syms),
            "with_readings": len(rows),
            "hot": len(hot),
            "gates": {
                "min_plus_di": s.tradier_hot_min_pdi,
                "di_ratio": s.tradier_hot_di_ratio,
                "min_adx": s.tradier_hot_min_adx,
            },
            "took_s": round(time.time() - started, 1),
            "venue": "live" if live else "sandbox",
            "at": f"{datetime.now(CST):%H:%M:%S}",
        },
    }


def _refresh_async(user, live: bool, interval: str) -> None:
    uid = user.user_id
    with _LOCK:
        if uid in _REFRESHING:
            return
        _REFRESHING.add(uid)

    def run():
        try:
            got = _refresh(user, live, interval)
            _CACHE[uid] = {"at": time.monotonic(), "day": got["meta"]["day"],
                           "interval": interval, **got}
        except Exception as exc:                          # noqa: BLE001
            logger.warning("hot scan refresh failed: %s", exc)
            cached = _CACHE.get(uid)
            if cached is not None:
                cached["error"] = str(exc)[:200]
        finally:
            with _LOCK:
                _REFRESHING.discard(uid)

    threading.Thread(target=run, daemon=True, name=f"hotscan-{uid[:8]}").start()


def snapshot(user, *, live: bool = False, interval: str | None = None,
             force: bool = False) -> dict:
    """Last good HOT snapshot; refreshes in the background when stale.

    Returns immediately either way — the first call of a session comes back
    empty with ``refreshing: true`` rather than blocking for 100 calls.
    """
    s = get_settings()
    interval = interval or s.tradier_hot_interval
    uid = user.user_id
    cached = _CACHE.get(uid)
    fresh = (cached is not None
             and cached.get("day") == _today()
             and cached.get("interval") == interval
             and time.monotonic() - cached["at"] < s.tradier_hot_ttl_s)
    if force or not fresh:
        _refresh_async(user, live, interval)
    with _LOCK:
        refreshing = uid in _REFRESHING
    if cached is None or cached.get("interval") != interval:
        return {"rows": [], "meta": {"day": _today(), "interval": interval},
                "refreshing": refreshing, "age_s": None}
    return {
        "rows": cached["rows"],
        "meta": cached["meta"],
        "error": cached.get("error"),
        "refreshing": refreshing,
        "age_s": int(time.monotonic() - cached["at"]),
    }
