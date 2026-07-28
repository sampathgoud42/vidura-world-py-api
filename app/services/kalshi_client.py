"""Minimal synchronous Kalshi trade-api/v2 client (RSA-PSS request signing).

Ported from the canonical async client in the 38trades repo
(v4_bot_kalshi_btc15.py) with the import-time env reads converted to
constructor parameters so one API process can serve many users.

Signature scheme: RSA-PSS(SHA256, MGF1-SHA256, salt=digest length) over
``f"{timestamp_ms}{METHOD}/trade-api/v2{path}"`` — the bare path only,
query params are never signed.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"


class KalshiAuthError(Exception):
    pass


class KalshiApiError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Kalshi API {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


class KalshiClient:
    def __init__(
        self,
        api_key_id: str,
        private_key_path: str | Path,
        base_uri: str = DEFAULT_BASE,
        *,
        pem_password: str | None = None,
        timeout: float = 15.0,
    ):
        self.api_key_id = api_key_id
        self.base_uri = base_uri.rstrip("/")
        self.timeout = timeout
        raw = Path(private_key_path).read_bytes()
        password = pem_password.encode() if pem_password else None
        try:
            key = serialization.load_pem_private_key(raw, password=password)
        except TypeError as exc:
            # Key is encrypted but no/who password given (or vice versa).
            raise KalshiAuthError(f"PEM password problem: {exc}") from exc
        except ValueError as exc:
            raise KalshiAuthError(f"Could not parse private key PEM: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiAuthError("Private key is not an RSA key")
        self._key = key
        self._session = requests.Session()

    # -- signing --------------------------------------------------------
    def _sign(self, ts_ms: str, method: str, path: str) -> str:
        message = f"{ts_ms}{method}{API_PREFIX}{path}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, path),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        url = self.base_uri + path
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(method.upper(), path),
                    timeout=self.timeout,
                )
                if resp.status_code < 400:
                    return resp.json() if resp.content else {}
                # 4xx other than 429 will not improve on retry.
                if resp.status_code != 429 and resp.status_code < 500:
                    raise KalshiApiError(resp.status_code, resp.text)
                last_exc = KalshiApiError(resp.status_code, resp.text)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
            time.sleep(0.4 * attempt)
        raise last_exc if last_exc else RuntimeError("unreachable")

    # -- read-only convenience wrappers ---------------------------------
    def balance_cents(self) -> int:
        return int(self.request("GET", "/portfolio/balance").get("balance", 0))

    def exchange_status(self) -> dict[str, Any]:
        return self.request("GET", "/exchange/status")

    def positions(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self.request("GET", "/portfolio/positions", params=params)
        return data.get("market_positions", [])

    def fills(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.request("GET", "/portfolio/fills", params=params).get("fills", [])

    def settlements(self, limit: int = 100) -> list[dict]:
        return self.request("GET", "/portfolio/settlements", params={"limit": limit}).get(
            "settlements", []
        )

    def close(self) -> None:
        self._session.close()
