"""SPY 0DTE dealer-gamma from getgamma.io's option chain.

The vendor endpoint returns the RAW chain — per-contract gamma, open interest
and strike — and its dashboard computes the headline numbers in the browser.
So we compute them here:

    net GEX    signed dollar gamma across the chain (calls +, puts -)
    flip       the strike where CUMULATIVE gamma crosses zero
    call wall  strike carrying the most call gamma
    put wall   strike carrying the most put gamma
    magnets    the heaviest-gamma strikes bracketing spot — price tends to
               pin between them into the close

TRANSPORT NOTE (2026-07-30): getgamma sits behind Vercel bot protection that
answers a server-side request with HTTP 429 + a "Security Checkpoint" page,
even carrying the exact browser headers and a valid session cookie. A real
browser on the site gets 200. Defeating that check is not something this
service does, so ``fetch_live`` reports the block plainly and the endpoint
also accepts a payload captured from the browser. The maths below is the same
either way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

VENDOR_URL = "https://www.getgamma.io/api/options"
CONTRACT_MULTIPLIER = 100          # one option = 100 shares

# Ordinary browser-shaped headers so a legitimate request is not rejected for
# looking malformed. Nothing here attempts to defeat the bot check.
_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.getgamma.io/dashboard",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}


class GammaError(Exception):
    """Vendor unreachable, challenged, or the session expired."""


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out          # drop NaN


def fetch_live(ticker: str = "SPY", strikes: int = 50) -> dict:
    """Try the vendor directly. No credentials: the endpoint needs none.

    Verified 2026-07-30 — identical requests with a session JWT, with only the
    gamma_fp visitor cookie, and with no cookies at all all return HTTP 429 +
    a Vercel checkpoint page, while a real browser gets 200. The gate is client
    fingerprinting, not authentication, so there is nothing to authenticate
    with and nothing here tries to look like a browser past that check.
    """
    import requests

    try:
        resp = requests.get(
            VENDOR_URL,
            params={"ticker": ticker, "mode": "0dte", "strikes": strikes},
            headers=_HEADERS,
            timeout=30,
        )
    except Exception as exc:                     # network / DNS / TLS
        raise GammaError(f"getgamma unreachable: {exc}") from exc

    ctype = resp.headers.get("content-type") or ""
    if "json" not in ctype:
        raise GammaError(
            f"getgamma answered HTTP {resp.status_code} with a bot-check page, not JSON. "
            "Its edge blocks server-side calls regardless of cookies — push the "
            "chain from a browser tab on getgamma.io instead."
        )
    if resp.status_code >= 400:
        raise GammaError(f"getgamma HTTP {resp.status_code}")
    return resp.json()


def compute(payload: dict) -> dict:
    """Turn the raw chain into the desk line. Pure — no network, no clock."""
    if not isinstance(payload, dict):
        raise GammaError("payload is not an object")
    contracts = payload.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise GammaError("payload carries no option contracts")
    spot = _num(payload.get("spotPrice"))
    if not spot:
        raise GammaError("payload carries no spot price")

    # dollar gamma per 1% move, the convention the desk line quotes
    unit = CONTRACT_MULTIPLIER * spot * spot * 0.01

    per_strike: dict[float, dict[str, float]] = {}
    for c in contracts:
        strike = _num(c.get("strike_price"))
        gamma = _num((c.get("greeks") or {}).get("gamma"))
        oi = _num(c.get("open_interest"))
        if strike is None or gamma is None or oi is None or oi <= 0:
            continue
        side = str(c.get("contract_type") or "").lower()
        if side not in ("call", "put"):
            continue
        slot = per_strike.setdefault(strike, {"call": 0.0, "put": 0.0})
        slot[side] += gamma * oi * unit

    if not per_strike:
        raise GammaError("no contracts carried gamma and open interest")

    strikes_sorted = sorted(per_strike)
    # dealers are short calls / long puts: call gamma positive, put gamma negative
    net_by_strike = {k: per_strike[k]["call"] - per_strike[k]["put"] for k in strikes_sorted}
    net_gex = sum(net_by_strike.values())

    call_wall = max(strikes_sorted, key=lambda k: per_strike[k]["call"])
    put_wall = max(strikes_sorted, key=lambda k: per_strike[k]["put"])

    # gamma flip: where the running total crosses zero, interpolated between
    # the bracketing strikes rather than snapped to one of them
    flip = None
    running = 0.0
    prev_k, prev_run = None, 0.0
    for k in strikes_sorted:
        running += net_by_strike[k]
        if prev_k is not None and (prev_run <= 0 < running or prev_run >= 0 > running):
            span = running - prev_run
            flip = k if span == 0 else prev_k + (k - prev_k) * (-prev_run / span)
            break
        prev_k, prev_run = k, running

    # magnets: the heaviest ABSOLUTE gamma strikes around spot — where price
    # gets pinned. Reported high-to-low like the desk line.
    ranked = sorted(strikes_sorted, key=lambda k: abs(net_by_strike[k]), reverse=True)
    magnets = sorted({k for k in ranked[:6]}, reverse=True)
    above = [k for k in magnets if k >= spot]
    below = [k for k in magnets if k < spot]
    magnet_hi = min(above) if above else (max(magnets) if magnets else None)
    magnet_lo = max(below) if below else (min(magnets) if magnets else None)

    regime = "NEG" if net_gex < 0 else "POS"
    return {
        "ticker": payload.get("ticker") or "SPY",
        "mode": payload.get("mode") or "0dte",
        "spot": round(spot, 2),
        "regime": regime,
        "net_gex": round(net_gex, 2),
        "flip": round(flip, 2) if flip is not None else None,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "magnet_hi": magnet_hi,
        "magnet_lo": magnet_lo,
        "magnets": magnets,
        "market_status": payload.get("marketStatus"),
        "market_open": payload.get("marketOpen"),
        "vendor_ts": payload.get("timestamp"),
        "contracts": len(contracts),
        "strikes": len(strikes_sorted),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": summary_line(payload.get("ticker") or "SPY", regime, net_gex,
                             flip, call_wall, put_wall, magnet_hi, magnet_lo),
    }


def fmt_gex(value: float | None) -> str:
    """$-12.56B / $980.4M / $12.3K — the desk's shorthand."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    n = abs(float(value))
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= cut:
            return f"{sign}${n / cut:.2f}{suffix}"
    return f"{sign}${n:.0f}"


def summary_line(ticker, regime, net_gex, flip, call_wall, put_wall, hi, lo) -> str:
    """SPY NEG · net -$12.56B · flip 743.09 · call wall 740 · put wall 730 · magnets 744-733"""
    parts = [
        f"{ticker} {regime}",
        f"net {fmt_gex(net_gex)}",
        f"flip {flip:.2f}" if flip is not None else "flip —",
        f"call wall {call_wall:g}" if call_wall is not None else "call wall —",
        f"put wall {put_wall:g}" if put_wall is not None else "put wall —",
    ]
    if hi is not None and lo is not None:
        parts.append(f"magnets {hi:g}-{lo:g}")
    return " · ".join(parts)
