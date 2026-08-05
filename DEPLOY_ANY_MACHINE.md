# Deploying Vidura World on any machine (Linux · macOS · Windows)

The stack is three pieces, all portable:

| piece | tech | default port | state |
| --- | --- | --- | --- |
| Database | single SQLite file (Postgres optional for cloud) | — | `VIDURA_DATABASE_PATH` |
| Backend | FastAPI + uvicorn (`vidura-world/`) | 8790 | reads `.env` next to it |
| Frontend | React + Vite (`vidura-world-js/`) | 5173 dev / any static host | build-time or URL config |

Every machine-specific value lives in config, not code: the backend reads a
`.env` (template: `.env.example`), the frontend auto-discovers or is told its
API. **Code defaults are portable and paper-safe** — a fresh deployment can
never place live orders or touch another machine's paths by accident.

---

## 1 · Prerequisites

- **Python 3.11+** (3.12/3.14 fine) with `venv`
- **Node 18+** with npm
- Windows only: nothing extra (tzdata ships via the `tzdata` pip package in
  requirements). Linux/macOS: system tzdata is already there.
- Optional: **Flyway CLI** for DB migration bookkeeping (the app also
  auto-creates its schema on first boot, so Flyway is recommended, not
  required).

## 2 · Database

SQLite — a single file; put it anywhere and point the backend at it.

```bash
# choose a home for the data, e.g.
mkdir -p /srv/vidura/data          # (Windows: any folder)
```

- **Fresh machine, no Flyway**: skip ahead — the backend creates all tables
  on first boot. Then (recommended) apply the trigger migration so manual
  inserts auto-fill IDs/timestamps:
  `database/flyway/sql/V2__autofill_id_and_timestamp_triggers.sql` via
  `sqlite3 app.db < V2__...sql` or any SQLite tool.
- **With Flyway**: copy the `database/flyway/` folder, edit `flyway.conf`'s
  `flyway.url=jdbc:sqlite:<your path>/app.db` (this line is per-machine by
  nature), then `flyway migrate` — V1 schema, V2 triggers, V3 tradier column.
- **Maintenance**: `database/cleanup_old_data.py` ages out old rows; it
  operates on the `app.db` sitting NEXT TO the script, so keep a copy of the
  script beside the DB (or pass through — see its `--help`).
- **Cloud/Postgres**: set `VIDURA_DATABASE_URL_OVERRIDE=postgresql+psycopg://…`
  and ignore the SQLite path entirely.

## 3 · Backend (vidura-world)

```bash
cd vidura-world
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate && pip install -r requirements.txt
# Windows (PowerShell):
#   .venv\Scripts\Activate.ps1 ; pip install -r requirements.txt

cp .env.example .env     # then edit — every value optional
```

Minimal `.env` for a new machine:

```ini
VIDURA_DATABASE_PATH=/srv/vidura/data/app.db
VIDURA_CUSTOMERS_ROOT=/srv/vidura/customers
```

Run it:

- Linux/macOS: `./run.sh` (or `uvicorn app.main:app --host 0.0.0.0 --port 8790`)
- Windows: `run.bat`
- As a service: systemd unit / launchd plist / Task Scheduler entry that runs
  the same command with the project folder as working directory (the `.env`
  is found relative to it).

Smoke test: `curl http://localhost:8790/health` →
`{"status":"ok", ..., "paper_only": true}`.

### Key backend settings (all optional, env prefix `VIDURA_`)

| variable | default | meaning |
| --- | --- | --- |
| `VIDURA_DATABASE_PATH` | `<project>/var/app.db` | SQLite file |
| `VIDURA_CUSTOMERS_ROOT` | `<project>/customers` | per-user secrets folders |
| `VIDURA_PAPER_ONLY` | `true` | **safety gate** — live orders only when `false` |
| `VIDURA_SUPER_PYTHON` | this venv | interpreter for signal engines (needs yfinance/pandas) |
| `VIDURA_LEVELS_DIR` | `<project>/runtime/stock-trade` | optional level-cross watcher home; absent = feature degrades gracefully |
| `VIDURA_API_KEY` | *(open)* | require `X-API-Key` on every request |
| `VIDURA_PORT` | `8790` | port used by run.sh / run.bat |
| `VIDURA_TRADIER_*` | see `app/core/config.py` | Tradier desk + auto-trader knobs |

Every field in `app/core/config.py` maps to `VIDURA_<FIELDNAME>` — the file
is the authoritative reference.

## 4 · Frontend (vidura-world-js)

```bash
cd vidura-world-js
npm install
```

- **Development**: `npm run dev` → http://localhost:5173. When served from
  port 5173/4173 it assumes the API at `http://<same host>:8790`
  automatically.
- **Production**: `npm run build` → static `dist/` for any web server, CDN
  or Netlify. Tell it where the API is, one of:
  - build-time: `VITE_VIDURA_API=https://api.example.com npm run build`
  - runtime, per browser: open the app once with `?api=https://api.example.com`
    (persisted; `?api=off` clears)
  - reverse proxy `/api` + `/health` to the backend and build with no var
    (same-origin mode).
- **Operator identity** (which user the desks act as): defaults to
  `sampath`; override per deployment with `VITE_VIDURA_OPERATOR=<name>` at
  build time, or per browser with `?operator=<name>` (persisted,
  `?operator=off` clears).

## 5 · Bots & engines

The trading bots and signal engines are vendored inside the backend under
`runtime/` — they deploy with it, launched by the API with the right cwd and
env. Nothing references a developer checkout. Two notes:

- The **levels watcher** (SPY/QQQ/SPX crosses feeding the Tradier
  auto-trader) is optional; point `VIDURA_LEVELS_DIR` at a folder containing
  `levels_watcher.py` to enable it. There must be exactly ONE instance per
  ledger — don't run it from two folders.
- The engines' extra deps (`yfinance`, `pandas`) must exist in whichever
  interpreter `VIDURA_SUPER_PYTHON` points to (default: the API's venv — so
  `pip install yfinance pandas` there is the simplest route).

## 6 · Onboarding a new user

1. **Create the secrets folder** `<VIDURA_CUSTOMERS_ROOT>/<username>/` with:
   - `kalshi.env` / `.env` — the user's Kalshi API key id (see
     `app/services/credentials.py` for the exact expected keys)
   - `kalshi_private.pem` — their Kalshi RSA private key
   - `.sam` — the app password file (plain text password, used by the
     wellness/sports login contract)
   - optional `TRADIER_SANDBOX_TOKEN` / `TRADIER_SANDBOX_ACCOUNT_ID`
     (+ `TRADIER_ACCESS_TOKEN` / `TRADIER_ACCOUNT_ID` for live) in the
     folder's `.env` for the options desk
2. **Register the user** — either open the app with `?operator=<username>`
   (it self-registers on first load), or explicitly:
   ```bash
   curl -X POST http://localhost:8790/api/v1/users \
     -H "Content-Type: application/json" \
     -d '{"username": "<username>", "email": "user@example.com"}'
   ```
   The server derives the folder from `VIDURA_CUSTOMERS_ROOT` — never send
   machine paths from clients. (A custom folder CAN be passed, but must live
   under the customers root unless `VIDURA_ALLOW_ANY_ROOT=true`.)
3. **Verify the handshake**: `POST /api/v1/users/<user_id>/kalshi-client`
   → `authenticated: true` + balance. 424 = credentials missing, 422 = bad
   PEM.
4. The user's world is live: portfolio, bots, trades and wellness data all
   key off that `user_id`.

## 7 · Go-live checklist (per machine, deliberate)

1. Everything works in paper (`paper_only: true` in `/health`).
2. Credentials verified via the kalshi-client handshake.
3. Set `VIDURA_PAPER_ONLY=false` in that machine's `.env`, restart the API.
4. `/health` now reports `paper_only: false` — the desks show LIVE unlocked.
   Only now can bots/desks place real-money orders, and only when a launch
   explicitly selects LIVE mode.

## 8 · This workstation (reference deployment)

The original Windows workstation keeps its exact behavior through
`vidura-world/.env` (DB at `D:/_projects/database/app.db`, customers at
`D:/_projects/customers`, system Python for engines, levels watcher in the
38trades repo, live trading unlocked). That file IS the machine — the code
no longer knows anything about it.
