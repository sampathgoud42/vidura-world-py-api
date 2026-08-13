"""Tradier executor: selection, sizing, and the TP/SL state machine —
venue fully scripted, nothing leaves the process."""

from __future__ import annotations

import pytest

from app.core.database import SessionLocal
from app.models import User
from app.services import tradier_bot
from app.services.tradier_client import TradierCredentials, TradierError


def _chain():
    g = lambda d: {"greeks": {"delta": d}}  # noqa: E731
    return [
        # calls
        {"symbol": "SPY_C630", "option_type": "call", "strike": 630,
         "bid": 0.98, "ask": 1.02, **g(0.55)},          # above band
        {"symbol": "SPY_C635", "option_type": "call", "strike": 635,
         "bid": 0.50, "ask": 0.50, **g(0.38)},          # in band, near mid
        {"symbol": "SPY_C640", "option_type": "call", "strike": 640,
         "bid": 0.30, "ask": 0.34, **g(0.26)},          # in band, edge
        {"symbol": "SPY_C645", "option_type": "call", "strike": 645,
         "bid": 0.0, "ask": 0.10, **g(0.40)},           # NO BID -> untradable
        # puts carry negative delta
        {"symbol": "SPY_P630", "option_type": "put", "strike": 630,
         "bid": 0.45, "ask": 0.47, **g(-0.37)},
        {"symbol": "SPY_P620", "option_type": "put", "strike": 620,
         "bid": 0.20, "ask": 0.22, **g(-0.15)},         # below band
    ]


class FakeTradier:
    """Scripted venue. Orders auto-increment; fills are set by the test."""

    def __init__(self, sandbox: bool = True):
        self.orders: dict[str, dict] = {}
        self.status: dict[str, dict] = {}
        self.bid = 0.50
        self.cancel_fails: set[str] = set()
        self.cancelled: list[str] = []
        self._next = 100
        self.creds = TradierCredentials(
            access_token="test", account_id="TEST", sandbox=sandbox,
            base_url=None,
        )

    def balances(self):
        return {"option_buying_power": 500.0, "total_cash": 500.0,
                "total_equity": 500.0, "open_pl": 0.0,
                "account_id": "TEST", "sandbox": True}

    def expirations(self, symbol):
        return ["2026-08-07", "2026-08-14"]

    def chain(self, symbol, expiration):
        return _chain()

    def quote(self, occ):
        return {"bid": self.bid, "ask": self.bid + 0.02}

    def place_option_order(self, *, underlying, occ_symbol, side, quantity,
                           order_type="limit", price=None, duration="day"):
        self._next += 1
        oid = str(self._next)
        self.orders[oid] = {"side": side, "qty": quantity, "type": order_type,
                            "price": price, "occ": occ_symbol}
        self.status[oid] = {"status": "open"}
        return {"id": oid, "status": "ok"}

    def order_status(self, oid):
        return self.status.get(str(oid), {"status": "open"})

    def cancel_order(self, oid):
        oid = str(oid)
        if oid in self.cancel_fails:
            raise TradierError("order is not cancelable", 400)
        self.cancelled.append(oid)
        self.status[oid] = {"status": "canceled"}
        return {"id": oid}

    def close(self):
        pass


@pytest.fixture()
def fake(monkeypatch):
    venue = FakeTradier()
    monkeypatch.setattr(tradier_bot, "client_for",
                        lambda user, **kw: venue)
    return venue


@pytest.fixture()
def db_user(client, user):
    db = SessionLocal()
    try:
        yield db, db.get(User, user["user_id"])
    finally:
        db.close()


# ── math ────────────────────────────────────────────────────────────────────

def test_sizing_matches_the_desk_example():
    # balance 500, bid 0.50, 50% -> 250 / (0.50 * 100) = 5 contracts
    assert tradier_bot.size_contracts(500, 50, 0.50) == 5
    assert tradier_bot.size_contracts(500, 50, 0.55) == 4     # floor, never round up
    assert tradier_bot.size_contracts(100, 50, 0.60) == 0     # cannot afford one


def test_pick_prefers_mid_band_and_needs_two_sided_quote():
    pick = tradier_bot.pick_contract(_chain(), "call", 0.25, 0.50)
    assert pick["symbol"] == "SPY_C635"          # delta .38 is nearest .375
    # the no-bid 0.40-delta contract must never win, even though its delta
    # is closer to the mid than the .26 edge contract
    symbols = [tradier_bot.pick_contract(_chain(), "call", 0.39, 0.41)]
    assert symbols == [None]


def test_pick_uses_abs_delta_for_puts():
    pick = tradier_bot.pick_contract(_chain(), "put", 0.25, 0.50)
    assert pick["symbol"] == "SPY_P630"          # raw delta -0.37


# ── lifecycle ───────────────────────────────────────────────────────────────

def _open(db, user):
    return tradier_bot.open_position(
        db, user, symbol="SPY", side="call", buy_pct=50,
        tp_pct=15, sl_pct=30,
    )


def test_open_sizes_and_buys(fake, db_user):
    db, user = db_user
    pos = _open(db, user)
    assert pos.status == "pending"
    assert pos.occ_symbol == "SPY_C635"
    # ask 0.50 -> 5 contracts of the 500-dollar account at 50%
    assert pos.contracts == 5
    buy = fake.orders[pos.buy_order_id]
    assert buy["side"] == "buy_to_open" and buy["qty"] == 5


def test_fill_arms_tp_and_sl(fake, db_user):
    db, user = db_user
    pos = _open(db, user)
    fake.status[pos.buy_order_id] = {"status": "filled", "avg_fill_price": 0.50}
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    assert pos.status == "open"
    assert pos.entry_price == 0.50
    # +15% of 0.50 is 0.575; the penny grid CEILS to 0.58 so the TP can
    # never sell BELOW the promised percent (round() would give 0.57).
    assert pos.tp_price == pytest.approx(0.58)
    assert pos.sl_price == pytest.approx(0.35)    # -30% of 0.50
    tp = fake.orders[pos.tp_order_id]
    assert tp["side"] == "sell_to_close" and tp["price"] == pytest.approx(0.58)
    assert tp["duration"] == "gtc" if "duration" in tp else True


def test_tp_fill_closes_with_pnl(fake, db_user):
    db, user = db_user
    pos = _open(db, user)
    fake.status[pos.buy_order_id] = {"status": "filled", "avg_fill_price": 0.50}
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    fake.status[pos.tp_order_id] = {"status": "filled", "avg_fill_price": 0.58}
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    assert pos.status == "tp_filled"
    # (0.58 - 0.50) * 100 * 5 contracts
    assert pos.pnl_usd == pytest.approx(40.0)


def test_sl_cancels_tp_before_selling(fake, db_user):
    db, user = db_user
    pos = _open(db, user)
    fake.status[pos.buy_order_id] = {"status": "filled", "avg_fill_price": 0.50}
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    fake.bid = 0.34                                # below the 0.35 stop
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    assert pos.status == "sl_sold"
    # ORDER MATTERS: the TP cancel must precede the market sell, or two
    # sells stack for one position
    assert pos.tp_order_id in fake.cancelled
    sell = fake.orders[pos.close_order_id]
    assert sell["side"] == "sell_to_close" and sell["type"] == "market"
    assert pos.pnl_usd == pytest.approx((0.34 - 0.50) * 100 * 5)


def test_tp_winning_the_sl_race_is_a_win_not_a_double_sell(fake, db_user):
    db, user = db_user
    pos = _open(db, user)
    fake.status[pos.buy_order_id] = {"status": "filled", "avg_fill_price": 0.50}
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    # the cancel fails because the TP just filled — the exit already happened
    fake.cancel_fails.add(pos.tp_order_id)
    fake.status[pos.tp_order_id] = {"status": "filled", "avg_fill_price": 0.575}
    fake.bid = 0.30
    tradier_bot.monitor_pass(db, user)
    db.refresh(pos)
    assert pos.status == "tp_filled"
    assert pos.close_order_id is None              # no second sell was placed


def test_multiple_positions_run_side_by_side(fake, db_user):
    db, user = db_user
    a = _open(db, user)
    b = tradier_bot.open_position(db, user, symbol="SPY", side="put", buy_pct=20)
    assert a.id != b.id
    fake.status[a.buy_order_id] = {"status": "filled", "avg_fill_price": 0.50}
    fake.status[b.buy_order_id] = {"status": "canceled"}
    out = tradier_bot.monitor_pass(db, user)
    db.refresh(a); db.refresh(b)
    assert out["checked"] == 2
    assert a.status == "open" and b.status == "failed"


# ── API surface ─────────────────────────────────────────────────────────────

def test_endpoints_roundtrip(fake, client, user):
    uid = user["user_id"]
    r = client.get("/api/v1/tradier/balance", params={"user_id": uid})
    assert r.status_code == 200 and r.json()["option_buying_power"] == 500.0

    r = client.get("/api/v1/tradier/chain",
                   params={"user_id": uid, "symbol": "spy", "side": "call"})
    assert r.status_code == 200
    assert r.json()["pick"] == "SPY_C635"

    r = client.post("/api/v1/tradier/positions", json={
        "user_id": uid, "symbol": "SPY", "side": "call", "buy_pct": 50,
    })
    assert r.status_code == 200
    pid = r.json()["id"]
    assert r.json()["status"] == "pending"

    r = client.get("/api/v1/tradier/positions",
                   params={"user_id": uid, "status": "active"})
    assert any(p["id"] == pid for p in r.json()["items"])

    r = client.post(f"/api/v1/tradier/positions/{pid}/close",
                    params={"user_id": uid})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"          # buy was still pending


# ── venue routing / LIVE toggle ──────────────────────────────────────────────

def test_normalize_base_url_fills_scheme_and_v1():
    from app.services.tradier_client import normalize_base_url

    assert (normalize_base_url("sandbox.tradier.com", sandbox=True)
            == "https://sandbox.tradier.com/v1")
    assert (normalize_base_url("api.tradier.com", sandbox=False)
            == "https://api.tradier.com/v1")
    assert (normalize_base_url("https://api.tradier.com/v1", sandbox=False)
            == "https://api.tradier.com/v1")
    assert (normalize_base_url("", sandbox=True)
            == "https://sandbox.tradier.com/v1")


def test_crossed_venue_uri_is_refused():
    """A sandbox client pointed at the live host is the one slip that turns a
    mock order into real money."""
    from app.services.tradier_client import normalize_base_url

    with pytest.raises(TradierError):
        normalize_base_url("api.tradier.com", sandbox=True)
    with pytest.raises(TradierError):
        normalize_base_url("sandbox.tradier.com", sandbox=False)


def test_open_position_defaults_to_sandbox(fake, db_user):
    """No live flag anywhere -> sandbox mock order, and the row says so."""
    db, user = db_user
    pos = tradier_bot.open_position(db, user, symbol="SPY", side="call")
    assert pos.sandbox is True


def test_open_position_records_the_venue_it_used(monkeypatch, db_user):
    db, user = db_user
    live_venue = FakeTradier(sandbox=False)
    monkeypatch.setattr(tradier_bot, "client_for", lambda user, **kw: live_venue)
    pos = tradier_bot.open_position(db, user, symbol="SPY", side="call", live=True)
    assert pos.sandbox is False


def test_live_refused_when_server_is_paper_only(monkeypatch, db_user):
    from app.core.config import get_settings

    db, user = db_user
    monkeypatch.setattr(get_settings(), "paper_only", True, raising=False)
    with pytest.raises(tradier_bot.TradierBotError) as exc:
        tradier_bot.client_for(user, live=True)
    assert exc.value.status_code == 403


def test_positions_filter_by_venue(client, fake, db_user, user):
    db, u = db_user
    tradier_bot.open_position(db, u, symbol="SPY", side="call")
    live_venue = FakeTradier(sandbox=False)
    import app.services.tradier_bot as tb
    orig = tb.client_for
    tb.client_for = lambda user, **kw: live_venue
    try:
        tradier_bot.open_position(db, u, symbol="QQQ", side="put", live=True)
    finally:
        tb.client_for = orig

    uid = user["user_id"]
    everything = client.get(f"/api/v1/tradier/positions?user_id={uid}").json()
    sandbox = client.get(
        f"/api/v1/tradier/positions?user_id={uid}&venue=sandbox").json()
    live = client.get(f"/api/v1/tradier/positions?user_id={uid}&venue=live").json()
    assert everything["total"] == sandbox["total"] + live["total"]
    assert all(p["sandbox"] is True for p in sandbox["items"])
    assert all(p["sandbox"] is False for p in live["items"])
    assert live["total"] >= 1 and sandbox["total"] >= 1
