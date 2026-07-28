"""Regression tests for issues found in the adversarial review."""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings


def test_api_key_guard_blocks_and_admits(client, user_folder):
    settings = get_settings()
    settings.api_key = "sekrit-key"
    try:
        r = client.get("/api/v1/users")
        assert r.status_code == 401
        r = client.get("/api/v1/users", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401
        r = client.get("/api/v1/users", headers={"X-API-Key": "sekrit-key"})
        assert r.status_code == 200
        # health + docs stay open
        assert client.get("/health").status_code == 200
    finally:
        settings.api_key = ""


def test_root_folder_restriction(client, tmp_path: Path):
    settings = get_settings()
    old_allow, old_root = settings.allow_any_root, settings.customers_root
    allowed_root = tmp_path / "customers"
    (allowed_root / "alice").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    settings.allow_any_root = False
    settings.customers_root = allowed_root
    try:
        r = client.post(
            "/api/v1/users",
            json={"username": "alice", "user_root_folder": str(allowed_root / "alice")},
        )
        assert r.status_code == 201
        r = client.post(
            "/api/v1/users",
            json={"username": "eve", "user_root_folder": str(outside)},
        )
        assert r.status_code == 422
        assert "must be inside" in r.json()["detail"]
    finally:
        settings.allow_any_root = old_allow
        settings.customers_root = old_root


def test_username_pattern_enforced(client, user_folder):
    r = client.post(
        "/api/v1/users",
        json={"username": "../evil", "user_root_folder": str(user_folder)},
    )
    assert r.status_code == 422


def test_stale_open_row_does_not_erase_settled_pnl(client, user, user_folder):
    """The same sports ledger can appear under two filenames; a stale OPEN
    copy must never downgrade a settled row."""
    header = (
        "ts_epoch,ts,ticker,player,situation,confidence,reason,set_num,"
        "signal_bid,buy_price,fill_price,contracts,tp_price,pv_entry,status,"
        "ts_close,pv_close,realized_pnl,pv_delta_pct"
    )
    open_row = (
        "1783482910.30,2026-07-07 22:55:10 CST,KXITFMATCH-X,Player A,"
        "E,high,test,2,52,57,54,50,68,378.25,OPEN,,,,"
    )
    closed_row = open_row.replace("OPEN,,,,", "CLOSED,2026-07-07 23:50:32,401.25,50.0,6.08")
    trade_dir = user_folder / "trade_history"
    uid = user["user_id"]

    # settled ledger first
    (trade_dir / "trade_history_sports.csv").write_text(f"{header}\n{closed_row}\n")
    r = client.post("/api/v1/bots/sports/sync-trades", params={"user_id": uid})
    assert r.json()["inserted"] == 1

    # stale OPEN copy of the same row under the glob-fallback filename
    (trade_dir / "trade_history.csv").write_text(f"{header}\n{open_row}\n")
    r = client.post("/api/v1/bots/sports/sync-trades", params={"user_id": uid})
    assert r.json()["inserted"] == 0  # same ts_epoch -> same trade, no dupe

    r = client.get("/api/v1/bots/sports/trades", params={"user_id": uid})
    item = r.json()["items"][0]
    assert r.json()["total"] == 1
    assert item["status"] == "won"
    assert item["pnl_usd"] == 50.0


def test_aware_datetime_normalized_to_utc(client, user):
    uid = user["user_id"]
    # +05:30 offset must be converted, not stripped: 10:00+05:30 == 04:30 UTC
    r = client.post(
        f"/api/v1/users/{uid}/trades",
        json={
            "bot_key": "manual",
            "ticker": "TZ-TEST",
            "opened_at": "2026-07-20T10:00:00+05:30",
        },
    )
    assert r.status_code == 201
    assert r.json()["opened_at"].startswith("2026-07-20T04:30:00")


def test_wellness_options_404_for_unknown_user(client):
    assert client.get("/api/v1/users/nope/wellness/options").status_code == 404


def test_sparse_duplicate_close_row_merges_not_clobbers(client, user, user_folder):
    """The legacy close-fallback appends a sparse CLOSED row (pnl 0.0) with
    the SAME ts_epoch+ticker as the real settled row. It must merge away,
    not zero the real loss or double-count the trade."""
    header = (
        "ts_epoch,ts,ticker,player,situation,confidence,reason,set_num,"
        "signal_bid,buy_price,fill_price,contracts,tp_price,pv_entry,status,"
        "ts_close,pv_close,realized_pnl,pv_delta_pct"
    )
    real_row = (
        "1783725972.479,2026-07-10 15:26:12 CST,KXWTAMATCH-DUP,Player B,"
        "E,high,test,2,52,57,54,50,68,378.25,CLOSED,2026-07-10 16:00:00,365.65,-12.60,-3.33"
    )
    sparse_row = (
        "1783725972.479,2026-07-10 16:00:01 CST,KXWTAMATCH-DUP,Player B,"
        "E,high,close-fallback,2,,,,,,,CLOSED,,,0.0,"
    )
    (user_folder / "trade_history" / "trade_history_sports.csv").write_text(
        f"{header}\n{real_row}\n{sparse_row}\n"
    )
    uid = user["user_id"]
    r = client.post("/api/v1/bots/sports/sync-trades", params={"user_id": uid})
    assert r.json()["inserted"] == 1  # one trade, not two

    r = client.get("/api/v1/bots/sports/trades", params={"user_id": uid})
    assert r.json()["total"] == 1
    item = r.json()["items"][0]
    assert item["status"] == "lost"
    assert item["pnl_usd"] == -12.60

    # idempotent on re-sync
    r = client.post("/api/v1/bots/sports/sync-trades", params={"user_id": uid})
    assert r.json()["inserted"] == 0
    r = client.get("/api/v1/bots/sports/trades", params={"user_id": uid})
    assert r.json()["items"][0]["pnl_usd"] == -12.60
