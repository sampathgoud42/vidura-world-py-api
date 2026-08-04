"""Thin synchronous client for the Tradier brokerage REST API.

Two environments, mapped onto the desk's paper/live contract:

    paper -> https://sandbox.tradier.com/v1   (Tradier's own paper venue)
    live  -> https://api.tradier.com/v1

The sandbox is a REAL separate venue with its own token, not a dry-run flag —
so a paper session can never leak an order into the live account by a flag
being read wrong. VIDURA_PAPER_ONLY therefore gates which BASE URL a client
may be built for, the same place the Kalshi bots gate live mode.

Quirk this module absorbs so callers never see it: Tradier collapses
single-element arrays into bare objects ("quotes.quote" is a dict for one
symbol, a list for two). ``_as_list`` normalizes every such site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

PROD_BASE = "https://api.tradier.com/v1"
SANDBOX_BASE = "https://sandbox.tradier.com/v1"
TIMEOUT_S = 20


class TradierError(RuntimeError):
    """Transport or API-level failure, with the HTTP status when known."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _as_list(node: Any) -> list:
    """Tradier returns one-element collections as a bare object."""
    if node is None or node == "null":
        return []
    return node if isinstance(node, list) else [node]


@dataclass
class TradierCredentials:
    access_token: str
    account_id: str
    sandbox: bool


class TradierClient:
    def __init__(self, creds: TradierCredentials):
        self.creds = creds
        self.base = SANDBOX_BASE if creds.sandbox else PROD_BASE
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {creds.access_token}",
            "Accept": "application/json",
        })

    # ── plumbing ────────────────────────────────────────────────────────────
    def _req(self, method: str, path: str, *, params: dict | None = None,
             data: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        try:
            r = self._s.request(method, url, params=params, data=data,
                                timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise TradierError(f"Tradier unreachable: {exc}") from exc
        if r.status_code >= 400:
            # Tradier sends error text bodies; keep them, they say WHY
            raise TradierError(
                f"Tradier HTTP {r.status_code}: {r.text[:300]}", r.status_code
            )
        try:
            return r.json() or {}
        except ValueError as exc:
            raise TradierError(f"Tradier sent non-JSON: {r.text[:200]}") from exc

    def close(self) -> None:
        self._s.close()

    # ── account ─────────────────────────────────────────────────────────────
    def profile(self) -> dict:
        return self._req("GET", "/user/profile").get("profile", {})

    def balances(self) -> dict:
        """Flat view of the numbers the desk needs.

        ``option_buying_power`` is the sizing base: margin accounts report it
        under balances.margin, cash accounts under balances.cash — and using
        total_cash on a margin account would size against money options
        cannot actually spend.
        """
        b = self._req(
            "GET", f"/accounts/{self.creds.account_id}/balances"
        ).get("balances", {}) or {}
        margin = b.get("margin") or {}
        cash = b.get("cash") or {}
        obp = (b.get("option_buying_power")
               or margin.get("option_buying_power")
               or cash.get("cash_available")
               or b.get("total_cash") or 0)
        return {
            "total_equity": float(b.get("total_equity") or 0),
            "total_cash": float(b.get("total_cash") or 0),
            "option_buying_power": float(obp or 0),
            "open_pl": float(b.get("open_pl") or 0),
            "account_id": self.creds.account_id,
            "sandbox": self.creds.sandbox,
        }

    # ── market data ─────────────────────────────────────────────────────────
    def expirations(self, symbol: str) -> list[str]:
        d = self._req("GET", "/markets/options/expirations",
                      params={"symbol": symbol})
        return [str(x) for x in _as_list((d.get("expirations") or {}).get("date"))]

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        d = self._req("GET", "/markets/options/chains",
                      params={"symbol": symbol, "expiration": expiration,
                              "greeks": "true"})
        return _as_list((d.get("options") or {}).get("option"))

    def quote(self, occ_symbol: str) -> dict:
        d = self._req("GET", "/markets/quotes", params={"symbols": occ_symbol})
        quotes = _as_list((d.get("quotes") or {}).get("quote"))
        if not quotes:
            raise TradierError(f"no quote for {occ_symbol}")
        return quotes[0]

    # ── orders ──────────────────────────────────────────────────────────────
    def place_option_order(self, *, underlying: str, occ_symbol: str, side: str,
                           quantity: int, order_type: str = "limit",
                           price: float | None = None,
                           duration: str = "day") -> dict:
        data = {
            "class": "option",
            "symbol": underlying,
            "option_symbol": occ_symbol,
            "side": side,                      # buy_to_open | sell_to_close
            "quantity": str(int(quantity)),
            "type": order_type,                # limit | market
            "duration": duration,              # day | gtc
        }
        if order_type == "limit":
            if price is None:
                raise TradierError("limit order needs a price")
            data["price"] = f"{price:.2f}"
        d = self._req("POST", f"/accounts/{self.creds.account_id}/orders",
                      data=data)
        order = d.get("order") or {}
        if not order.get("id"):
            raise TradierError(f"order not accepted: {d}")
        return order

    def order_status(self, order_id: int | str) -> dict:
        d = self._req(
            "GET", f"/accounts/{self.creds.account_id}/orders/{order_id}"
        )
        return d.get("order") or {}

    def cancel_order(self, order_id: int | str) -> dict:
        d = self._req(
            "DELETE", f"/accounts/{self.creds.account_id}/orders/{order_id}"
        )
        return d.get("order") or {}
