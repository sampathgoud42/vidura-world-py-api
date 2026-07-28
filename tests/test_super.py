from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import super_research as svc

CENTRAL_HEADER = (
    "logged_at_cst,category,ticker,book,direction,signal,confluence,signal_price,"
    "entry_ref,target_price,stop_price,stop_deadline_cst,target_deadline_cst,"
    "acc_tp_before_sl_pct,acc_strict_pct,bar_time_cst,repeats,hot,engines,eng_hot,"
    "tfs,econ,outcome,outcome_at"
)
WORKER_HEADER = (
    "logged_at_cst,book,engine,tf,signal,direction,confluence,window,noise_k,"
    "vol_mult,acc_strict_pct,acc_tp_before_sl_pct,bt_trades,bar_time_cst,"
    "signal_price,entry_ref,target_price,stop_price,stop_deadline_cst,"
    "target_deadline_cst,vwap,y_poc,pivot,atr,volume,vol_vs_med20"
)


@pytest.fixture
def super_dir(tmp_path: Path, monkeypatch) -> Path:
    """A realistic fake super_research tree; settings.source_repo points at
    its parent so settings.super_dir resolves here."""
    repo = tmp_path / "repo"
    sdir = repo / "super_research"
    (sdir / "archive").mkdir(parents=True)
    (sdir / "spy_research").mkdir()
    (sdir / "gex").mkdir()

    config = {
        "_comment": "test",
        "abookOnTop": True,
        "categories": {
            "etf": {
                "label": "ETF",
                "bot": "super_signal_bot.py",
                "session": "rth",
                "tickers": [
                    {
                        "id": "spy",
                        "label": "SPY",
                        "path": "spy_research",
                        "csv": "spy_intraday_signals.csv",
                        "img": "live-spy.webp",
                        "rules": "TP ±0.25%",
                        "enabled": True,
                    }
                ],
            },
            "crypto": {
                "label": "Crypto",
                "bot": "super_signal_bot.py",
                "session": "24x7",
                "tickers": [
                    {
                        "id": "btc",
                        "label": "BTC",
                        "path": "btc_research",
                        "csv": "btc_intraday_signals.csv",
                        "img": "live-btc.webp",
                        "rules": "24x7",
                        "enabled": False,
                    }
                ],
            },
        },
    }
    (sdir / "super_research.config").write_text(json.dumps(config, indent=2))
    (sdir / "super_signal_bot.py").write_text("# test stub\n")

    (sdir / "a_signals.csv").write_text(
        f"{CENTRAL_HEADER}\n"
        "2026-07-27 12:30:12,etf,GLD,A,SHORT,vwap_loss,2,373.92,373.92,372.99,374.85,"
        "14:35,14:35,100.0,66.7,2026-07-27 12:00,2,,30m+1h+2h+4h,4,5m+30m,,stop,14:20\n"
        "2026-07-27 09:35:26,etf,SPY,A,LONG,poc_bounce,1,737.97,737.97,739.81,736.13,"
        "13:35,13:35,90.0,80.0,2026-07-27 09:30,1,1,1h+2h,2,5m,,target,10:05\n"
    )
    (sdir / "b_signals.csv").write_text(
        f"{CENTRAL_HEADER}\n"
        "2026-07-27 13:56:09,etf,QQQ,B,LONG,adi_up,1,681.05,681.05,682.76,679.35,"
        "14:35,14:35,87.5,87.5,2026-07-27 13:50,1,,1h+2h+4h,3,5m,,target,14:05\n"
    )
    (sdir / "archive" / "a_signals.csv").write_text(
        f"{CENTRAL_HEADER}\n"
        "2026-07-20 13:00:28,etf,GLD,A,SHORT,vwap_loss,1,367.91,367.91,366.81,369.01,"
        "14:35,14:35,100.0,71.4,2026-07-20 12:10,2,,30m+1h+2h+3h+4h,5,5m+30m,,timeout,14:35\n"
    )
    (sdir / "spy_research" / "spy_intraday_signals.csv").write_text(
        f"{WORKER_HEADER}\n"
        "2026-07-27 09:35:26,A,4h,5m,poc_reject_dn,SHORT,1,am_0845_1130,0.0,0.8,100.0,"
        "100.0,6,2026-07-27 09:30,737.97,737.97,736.13,739.81,13:35,13:35,742.05,"
        "738.0,739.96,1.003,902388,1.57\n"
        "2026-07-27 10:35:26,B,1h,15m,vwap_reclaim,LONG,2,am_0845_1130,0.0,0.8,80.0,"
        "90.0,10,2026-07-27 10:30,738.50,738.50,740.35,736.65,13:35,13:35,742.05,"
        "738.0,739.96,1.003,802388,1.40\n"
    )
    (sdir / "gex_daily.json").write_text(
        json.dumps(
            {
                "fetched_at": "2026-07-27 09:20:57",
                "stale": False,
                "tickers": {
                    "SPY": {
                        "as_of": "2026-07-27T14:20:52.897Z",
                        "price": 740.48,
                        "net_gex": -9178182877,
                        "regime": "negative_gamma",
                        "gamma_flip": 745.11,
                        "call_wall": 750,
                        "put_wall": 740,
                        "gamma_note": "Dealers short gamma",
                        "atm_iv": 15.7,
                        "pc_ratio_volume": 0.354,
                    }
                },
                "macro": {"vix": 18.72, "fear_greed": 41},
            }
        )
    )
    (sdir / "econ_today.json").write_text(
        json.dumps(
            {
                "date": "2026-07-27",
                "events": [],
                "yields": {"10y": 4.651, "d10y_bp": -2.8},
                "note": "",
                "high_impact": False,
            }
        )
    )
    (sdir / "gex" / "2026-07-27_spy.json").write_text(json.dumps({"symbol": "SPY"}))

    settings = get_settings()
    monkeypatch.setattr(settings, "source_repo", repo)
    # Never scan or touch real machine processes from tests.
    monkeypatch.setattr(svc, "_scan_running", lambda force=False: {})
    svc._CHILDREN.clear()
    return sdir


def test_state_shape_matches_vite_contract(client, super_dir):
    resp = client.get("/api/v1/super/state")
    assert resp.status_code == 200
    state = resp.json()
    assert state["abookOnTop"] is True
    assert {c["key"] for c in state["categories"]} == {"etf", "crypto"}

    etf = next(c for c in state["categories"] if c["key"] == "etf")
    assert etf["session"] == "rth"
    assert etf["live"] is False and etf["pid"] is None
    spy = etf["tickers"][0]
    assert spy["enabled"] is True and spy["live"] is False
    # worker rows: newest first, ALL STRING values
    assert spy["rows"][0]["logged_at_cst"] == "2026-07-27 10:35:26"
    assert spy["rows"][0]["signal_price"] == "738.50"
    assert isinstance(spy["rows"][0]["volume"], str)

    crypto = next(c for c in state["categories"] if c["key"] == "crypto")
    assert crypto["tickers"][0]["rows"] == []  # disabled -> no rows

    # abook: only book A rows from enabled tickers, with src/cat added
    assert all(r["book"] == "A" for r in state["abook"])
    assert state["abook"][0]["src"] == "SPY" and state["abook"][0]["cat"] == "etf"

    # central feeds sorted desc, strings preserved
    assert state["aFeed"][0]["ticker"] == "GLD"
    assert state["aFeed"][0]["eng_hot"] == "4"
    assert state["aFeed"][0]["hot"] == ""
    assert state["bFeed"][0]["book"] == "B"

    # econ + gex verbatim
    assert state["econ"]["date"] == "2026-07-27"
    assert state["gex"]["tickers"]["SPY"]["regime"] == "negative_gamma"


def test_state_all_merges_archive(client, super_dir):
    base = client.get("/api/v1/super/state").json()
    assert len(base["aFeed"]) == 2
    full = client.get("/api/v1/super/state?all=1").json()
    assert len(full["aFeed"]) == 3
    assert any(r["eng_hot"] == "5" for r in full["aFeed"])  # legacy archive row


def test_state_config_unreadable_error_shape(client, super_dir):
    (super_dir / "super_research.config").write_text("{not json")
    state = client.get("/api/v1/super/state").json()
    assert set(state) == {"error"}
    assert state["error"].startswith("config unreadable:")


def test_config_toggle_roundtrip(client, super_dir):
    resp = client.post("/api/v1/super/config", json={"enabled": {"spy": False, "btc": True}})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    cfg = client.get("/api/v1/super/config").json()
    tickers = {
        t["id"]: t["enabled"]
        for c in cfg["categories"].values()
        for t in c["tickers"]
    }
    assert tickers == {"spy": False, "btc": True}
    # preserved untouched keys
    assert cfg["_comment"] == "test"
    # state reflects it: spy disabled -> no rows
    state = client.get("/api/v1/super/state").json()
    etf = next(c for c in state["categories"] if c["key"] == "etf")
    assert etf["tickers"][0]["rows"] == []


def test_compat_aliases_match_v1(client, super_dir):
    v1 = client.get("/api/v1/super/state").json()
    compat = client.get("/api/super/state").json()
    assert compat == v1
    resp = client.post("/api/super/config", json={"enabled": {"spy": True}})
    assert resp.json() == {"ok": True}


def test_gex_and_econ_endpoints(client, super_dir):
    gex = client.get("/api/v1/super/gex").json()
    assert gex["tickers"]["SPY"]["net_gex"] == -9178182877
    econ = client.get("/api/v1/super/econ").json()
    assert econ["high_impact"] is False

    (super_dir / "gex_daily.json").unlink()
    assert client.get("/api/v1/super/gex").status_code == 404


def test_sync_and_query_signals(client, super_dir):
    resp = client.post("/api/v1/super/sync")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signals"]["inserted"] == 4  # 2 A + 1 B + 1 archive A
    assert body["snapshots"]["upserted"] == 3  # gex + econ + gex_raw_spy

    page = client.get("/api/v1/super/signals", params={"book": "a"}).json()
    assert page["total"] == 3
    assert page["items"][0]["ticker"] == "GLD"
    assert page["items"][0]["grade"] == "4"
    assert page["items"][0]["raw"]["outcome"] == "stop"

    hot = client.get("/api/v1/super/signals", params={"grade_min": 4}).json()
    assert hot["total"] == 2  # eng_hot 4 + legacy 5

    # idempotent
    again = client.post("/api/v1/super/sync").json()
    assert again["signals"]["inserted"] == 0

    snaps = client.get("/api/v1/super/snapshots", params={"kind": "gex"}).json()
    assert len(snaps) == 1 and snaps[0]["snapshot_date"] == "2026-07-27"


def test_supervisor_start_stop_logic_mocked(client, super_dir, monkeypatch):
    spawned = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return FakeProc()

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)
    resp = client.post("/api/v1/super/on")
    assert resp.status_code == 200
    body = resp.json()
    # etf has an enabled ticker -> started; crypto has none enabled -> skipped
    assert body["started"] == ["etf"]
    assert body["already"] == []
    assert body["sequence"] == ["etf"]
    assert "--category" in spawned[0] and "etf" in spawned[0]

    # now etf is live via our child handle -> 'already'
    resp = client.post("/api/v1/super/on")
    assert resp.json()["already"] == ["etf"]

    state = client.get("/api/v1/super/state").json()
    etf = next(c for c in state["categories"] if c["key"] == "etf")
    assert etf["live"] is True and etf["pid"] == 4242
