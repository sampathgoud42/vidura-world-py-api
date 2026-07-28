"""End-to-end smoke test against a running API using a real user folder.

Usage:
    python scripts/smoke_live.py [--base http://127.0.0.1:8790] \
        [--folder D:/_projects/customers/sampath] [--password ...]

Safety: this script only ever
  - authenticates read-only against Kalshi (balance + exchange status),
  - records MOCK trades in the local SQLite ledger,
  - starts/stops one bot briefly in forced paper mode.
It never places an order on the exchange.
"""

from __future__ import annotations

import argparse
import sys
import time

import requests

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--folder", default="D:/_projects/customers/sampath")
    ap.add_argument("--password", default=None, help=".sam password to verify (optional)")
    ap.add_argument("--pem-password", default=None, help="PEM passphrase if the key is encrypted")
    ap.add_argument("--start-bot", action="store_true", help="also exercise a real bot start/stop in paper mode")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    api = base + "/api/v1"
    s = requests.Session()

    # --- health ---------------------------------------------------------
    r = s.get(base + "/health", timeout=10)
    check("health", r.ok and r.json().get("status") == "ok", str(r.json()))
    check("paper_only enforced", r.json().get("paper_only") is True)

    # --- user create/list ----------------------------------------------
    username = "sampath"
    r = s.get(api + "/users", timeout=10)
    check("getUsers", r.ok)
    existing = next((u for u in r.json() if u["username"] == username), None)
    if existing is None:
        r = s.post(
            api + "/users",
            json={"username": username, "email": "sumasamfamily@gmail.com", "user_root_folder": args.folder},
            timeout=10,
        )
        check("createUser", r.status_code == 201, r.text[:200])
        user = r.json()
    else:
        user = existing
        check("createUser (already exists)", True, user["user_id"])
    uid = user["user_id"]
    check("root_folder_exists", user.get("root_folder_exists", False) in (True, None))

    # --- kalshi client (read-only auth) ---------------------------------
    body = {"pem_password": args.pem_password} if args.pem_password else None
    r = s.post(f"{api}/users/{uid}/kalshi-client", json=body, timeout=45)
    if r.status_code == 200:
        state = r.json()
        check(
            "getKalshiClient authenticated",
            state["authenticated"] is True,
            f"balance_cents={state.get('balance_cents')} exchange_active={state.get('exchange_active')} key={state.get('api_key_id_masked')}",
        )
    else:
        check("getKalshiClient", False, f"HTTP {r.status_code}: {r.text[:300]}")

    # --- .sam password check --------------------------------------------
    if args.password:
        r = s.post(f"{api}/users/{uid}/verify-password", json={"password": args.password}, timeout=15)
        check("verify-password (.sam)", r.status_code == 200, r.text[:120])

    # --- wellness --------------------------------------------------------
    r = s.get(f"{api}/users/{uid}/wellness/profile", timeout=10)
    check("wellness profile import", r.ok, str(r.json())[:160])
    r = s.get(f"{api}/users/{uid}/wellness/options", timeout=10)
    check("wellness options", r.ok and "goals" in r.json())
    r = s.post(
        f"{api}/users/{uid}/wellness/data",
        json={"kind": "checkin", "payload": {"source": "smoke", "mood": "good"}},
        timeout=10,
    )
    check("wellness add data", r.status_code == 201)
    r = s.get(f"{api}/users/{uid}/wellness/data", params={"days": 60}, timeout=10)
    check("wellness 60d data", r.ok and len(r.json()) >= 1, f"{len(r.json())} entries")

    # --- mock trades -----------------------------------------------------
    mock = {
        "bot_key": "manual",
        "ticker": "KXBTCD-SMOKE-TEST",
        "side": "yes",
        "action": "buy",
        "contracts": 3,
        "price_cents": 55,
        "cost_usd": 1.65,
        "status": "open",
        "is_mock": True,
        "raw": {"source": "smoke_live.py"},
    }
    r = s.post(f"{api}/users/{uid}/trades", json=mock, timeout=10)
    check("record mock trade", r.status_code == 201, f"id={r.json().get('id')}")
    r = s.get(f"{api}/users/{uid}/trades", params={"bot_key": "manual"}, timeout=10)
    check("trade history query", r.ok and r.json()["total"] >= 1, f"total={r.json()['total']}")

    # --- CSV ingest ------------------------------------------------------
    r = s.post(f"{api}/bots/sports/sync-trades", params={"user_id": uid}, timeout=60)
    check("sports sync-trades", r.ok, str(r.json())[:200] if r.ok else r.text[:200])
    r = s.post(f"{api}/bots/btc/sync-trades", params={"user_id": uid}, timeout=60)
    check("btc sync-trades", r.ok, str([f['inserted'] for f in r.json()]) if r.ok else r.text[:200])
    r = s.get(f"{api}/bots/sports/performance", params={"user_id": uid}, timeout=10)
    check("sports performance", r.ok, str(r.json())[:200])
    r = s.get(f"{api}/bots/sports/active-bets", params={"user_id": uid}, timeout=10)
    check("sports active-bets", r.ok, f"{len(r.json())} open")

    # --- registry + tennis models ---------------------------------------
    r = s.get(api + "/bots", timeout=10)
    check("getBots registry", r.ok and {b["key"] for b in r.json()} == {"btc15", "btc60", "sports"})
    r = s.get(api + "/bots/sports/config", timeout=10)
    check("sports config", r.ok and r.json().get("paper_only_server") is True)
    r = s.get(api + "/models/tennis", timeout=10)
    check("tennis models", r.ok and len(r.json()) == 5)
    r = s.post(
        api + "/models/tennis/v5/predictions",
        json={"match_name": "Smoke vs Test", "pick": "Smoke", "probability": 0.61, "confidence": "high"},
        timeout=10,
    )
    check("record tennis prediction", r.status_code == 201)
    r = s.get(api + "/models/tennis/v5/predictions", timeout=10)
    check("tennis predictions feed", r.ok and len(r.json()) >= 1)

    # --- optional real-bot paper lifecycle ------------------------------
    if args.start_bot:
        r = s.post(f"{api}/bots/btc/start?bot=btc15", json={"user_id": uid, "version": "v2", "mode": "paper"}, timeout=30)
        check("btc15 v2 paper start", r.status_code == 201, r.text[:200])
        if r.status_code == 201:
            run_id = r.json()["id"]
            time.sleep(20)
            r = s.get(f"{api}/bots/btc/status", params={"user_id": uid}, timeout=10)
            btc15 = next(x for x in r.json() if x["bot_key"] == "btc15")
            check("btc15 status running", btc15["running"] is True)
            r = s.get(f"{api}/bots/btc/logs", params={"bot": "btc15", "user_id": uid, "lines": 40}, timeout=10)
            check("btc15 logs", r.ok and len(r.json()["lines"]) > 0, "\n    ".join(r.json()["lines"][-5:]))
            r = s.post(f"{api}/bots/btc/stop?bot=btc15", json={"user_id": uid, "run_id": run_id}, timeout=30)
            check("btc15 stop", r.ok, r.text[:120])

    print("\n--- summary ---")
    failed = [r for r in results if r[0] == FAIL]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
