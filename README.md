# Vidura World API

> **Deploying on a new machine (Linux/macOS/Windows)?** Follow [DEPLOY_ANY_MACHINE.md](DEPLOY_ANY_MACHINE.md) — portable defaults, `.env` configuration, and user onboarding. The paths below describe the original workstation and are ONE example configuration.

Multi-user REST backend for the 38trades ecosystem: Kalshi BTC bots (15-minute
and 60-minute), the multi-sport trading bot, tennis prediction models, and the
wellness app. FastAPI + SQLAlchemy + SQLite, designed to serve mobile and web
clients from one place.

- Source bots repo: `D:\_projects\38trades-py-claude` (unchanged; wrapped, not forked)
- Database: `D:\_projects\database\app.db` (SQLite, WAL mode)
- User secrets: per-user root folders (e.g. `D:\_projects\customers\sampath`)

## Quick start

```bat
cd D:\_projects\vidura-world
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat
```

`run.bat` starts this API alone. To bring up the **API and the web desk
together**, use the launcher in the frontend repo instead — it starts both,
skips the API if 8790 is already listening, and captures the access log:

```bat
cd D:\_projects\vidura-world-js
vidura.bat            :: or ./vidura.sh on macOS/Linux/Git Bash
```

Then open:

| URL | What |
| --- | --- |
| http://localhost:5173/super-signals-intraday | the desk (via the launcher) |
| http://127.0.0.1:8790/docs | Swagger UI (interactive) |
| http://127.0.0.1:8790/redoc | ReDoc |
| http://127.0.0.1:8790/openapi.json | OpenAPI 3 spec |
| http://127.0.0.1:8790/health | health + config check |

Run the tests:

```bat
.venv\Scripts\python -m pytest
```

## Configuration

Every setting is overridable with a `VIDURA_`-prefixed env var (or a `.env`
file next to `run.bat`):

| Env var | Default | Meaning |
| --- | --- | --- |
| `VIDURA_DATABASE_PATH` | `D:/_projects/database/app.db` | SQLite file |
| `VIDURA_SOURCE_REPO` | `D:/_projects/38trades-py-claude` | bots repo root |
| `VIDURA_CUSTOMERS_ROOT` | `D:/_projects/customers` | default user folders root |
| `VIDURA_PAPER_ONLY` | `true` | force paper/dry-run on every bot start |
| `VIDURA_BOT_PYTHON` | this venv's python | interpreter for bot subprocesses |

On Linux set e.g. `VIDURA_DATABASE_PATH=/home/app/data/app.db` — stored
user paths in either Windows (`D:\...`) or POSIX (`/home/...`) notation are
normalized transparently (see `app/core/paths.py`).

## Architecture

```
app/
├── main.py              FastAPI app factory (CORS, JSON errors, lifespan)
├── core/
│   ├── config.py        pydantic-settings, VIDURA_* env overrides
│   ├── database.py      engine/session, WAL pragmas, init_db
│   └── paths.py         cross-platform user-folder path layer + traversal guard
├── models/              SQLAlchemy ORM: User, Trade, BotRun,
│                        WellnessProfile, WellnessEntry, TennisPrediction
├── schemas/             Pydantic request/response models
├── services/
│   ├── credentials.py   per-user .env/PEM/.sam discovery (never touches os.environ)
│   ├── kalshi_client.py sync Kalshi client, RSA-PSS request signing
│   ├── bot_registry.py  bot catalogue: keys, versions, script paths
│   ├── bot_launcher.py  subprocess bootstrap (fixes legacy renamed-module import)
│   ├── bot_manager.py   start/stop/status/logs with safety invariants
│   ├── ingest.py        bot CSV -> SQLite trade ledger sync (idempotent)
│   ├── trades.py        ledger queries + performance aggregation
│   ├── wellness.py      profile JSON <-> SQLite sync + 60-day entries
│   └── tennis_models.py tennis model registry (ground truth from forensics)
└── api/v1/              routers: users, kalshi, bots, trades, wellness, models_tennis
tests/                   pytest E2E suite over TestClient + temp SQLite
```

## Database schema

| Table | Purpose |
| --- | --- |
| `users` | user_id (UUID), username, email, user_root_folder (canonical form), timestamps |
| `trades` | unified ledger for all bots + manual/mock trades; bot-specific extras in `raw` JSON |
| `bot_runs` | one row per launched bot process: pid, mode, status, log file |
| `wellness_profiles` | current wellness preferences (mirror of `wellness-profile.json`) |
| `wellness_entries` | time-stamped wellness data, queried over a rolling window (default 60 days) |
| `tennis_predictions` | stored model outputs served to clients |
| `super_signals` | A/B-book super_research signal history (live + archive ledgers) |
| `daily_snapshots` | daily GEX / econ JSON history (`gex`, `econ`, `gex_raw_<ticker>`, `gex0dte`, `super_config`, `regen`) — one row per (kind, date) |
| `gex0dte_hourly` | SPY 0DTE net gamma per CST trading hour, 08:00–16:00; last write in the hour wins |
| `pusher_heartbeats` | append-only, one row per 0DTE push cycle (pass or fail), kept 3 days |

The last two are append-per-slot on purpose. Everything else here is an
upsert, which means the database held no **cadence** — and that is precisely
why a 25-minute 0DTE stall could not be resolved into "the tab died" versus
"the tab was alive and every push was refused".

## API surface (all under `/api/v1`)

**Users** — `GET/POST /users`, `GET /users/{id}`,
`POST /users/{id}/verify-password` (.sam check),
`POST /users/{id}/kalshi-client` (authenticate from the user's folder,
read-only balance + exchange status).

**Bots** — `GET /bots` (registry: btc15 v2–v5, btc60 fable5/burst, sports
main/v1/v2). BTC: `GET /bots/btc/status`, `POST /bots/btc/start|stop?bot=btc15|btc60`,
`GET /bots/btc/logs`, `GET /bots/btc/trades`, `POST /bots/btc/sync-trades`.
Sports: `GET /bots/sports/config|status|logs|active-bets|performance|trades`,
`POST /bots/sports/start|stop|sync-trades`.

**Trades** — `POST /users/{id}/trades` (record, incl. mock),
`GET /users/{id}/trades` (filter by bot, status, days; paginated).

**Wellness** — `GET/PUT /users/{id}/wellness/profile` (auto-imports the
folder JSON on first read, writes back on update),
`GET/POST /users/{id}/wellness/data` (60-day window default),
`GET /users/{id}/wellness/options` (selection choices for app UIs).

**Per-trade risk (BTC)** — `POST /bots/btc/start` takes `tp_pct` (profit
target, % over entry) and `sl_pct` (stop loss, % below entry). The stop routes
to `KALSHI_STOP_PCT` (btc15 v2/v3/v4, which share one `_tp_sl`) and
`BTC60_SL_PCT` (btc60 burst uses a percent in place of its fixed 15c offset;
fable5 pins its learner so the stop cannot drift), and switches the btc15 stop
monitor on. It is **rejected** for btc15 v5, which has no stop loss by design —
it holds every position to settlement, so accepting one would leave the desk
advertising a stop that does not exist.

Each run records the config it was actually launched with in
`bot_runs.extra.config`, built from the resolved environment rather than the
request, so a knob a given engine never received cannot appear as though it
were in force. The desk renders it under the running engine.

**Tradier options executor** — `GET /tradier/balance` (equity + option
buying power), `GET /tradier/chain` (delta-band candidates and the pick,
before any money moves), `POST /tradier/positions` (select by |delta| in
`delta_min..delta_max` (default 0.25-0.50), size as `buy_pct`% of option
buying power — `floor(budget / (ask x 100))`, the x100 being the contract
multiplier — buy at the ask, then manage the exit), `GET /tradier/positions`,
`POST /tradier/positions/sweep`, `POST /tradier/positions/{id}/close`.

Exit contract: the take-profit (`tp_pct`, default 15) is a GTC limit sell
resting ON the venue, so it survives API restarts; the stop-loss (`sl_pct`,
default 30) is the API's 10-second monitor loop, which cancels the TP before
selling — two sells must never stack — and whose state lives in the
`tradier_positions` table so a restart resumes every watch. Exit prices are
CEILED to the penny: `round(0.575, 2)` is 0.57 under banker's rounding, a TP
that sells below its promised percent.

Environments are separate venues, not a flag: `VIDURA_PAPER_ONLY=true` pins
every client to `sandbox.tradier.com` using `TRADIER_SANDBOX_TOKEN` /
`TRADIER_SANDBOX_ACCOUNT_ID` from the customer `.env`; live uses
`TRADIER_ACCESS_TOKEN` / `TRADIER_ACCOUNT_ID` against `api.tradier.com` and
only once the server is unlocked. Paper cannot reach live money by
construction. The desk lives at `/tradier-platform` in the web app.

**Tennis models** — `GET /models/tennis`,
`GET/POST /models/tennis/{model_id}/predictions`.

**Super-research desk** — `GET /super/state` (full desk state for the
SuperSite app: categories/tickers with live worker rows, A/B signal feeds,
econ + GEX blobs; `?all=1` merges the archive ledgers),
`POST /super/on` / `POST /super/off` (start/stop category supervisors;
detached processes that survive API restarts), `GET/POST /super/config`
(ticker enable toggles), `GET /super/gex`, `GET /super/econ`,
`POST /super/econ/refresh`, `POST /super/sync` (forced full ingest),
`GET /super/sync/status` (background-loop health),
`GET /super/signals` (filter by book/category/ticker/grade/days),
`GET /super/snapshots?kind=gex|econ|gex_raw_spy` (daily history).
A background loop (on by default: `VIDURA_SUPER_AUTO_SYNC`, every
`VIDURA_SUPER_SYNC_INTERVAL=60` seconds) continuously mirrors **every**
generated signal — central A/B ledgers, archives, and all per-ticker
worker CSVs — plus the daily gex/econ snapshots into SQLite, skipping
unchanged files, so the database is the durable record without any
manual sync.
Legacy-compatible aliases at `/api/super/state|on|config` serve the exact
vite-middleware shapes, so the existing frontend can point straight here.

**Engine tuning** — `GET/POST /super/engine-pct` (per-category `TP_PCT`/
`SL_PCT`, the race target that defines a win; reports `mixed` rather than
averaging when a category's ticker folders disagree),
`GET/POST /super/engine-gates` (`A_TPSL`/`MIN_TPSL`, the tp-before-sl
admission gates — **desk-wide**, because they are module constants in
`engine_common.py` shared by every ticker),
`POST /super/tickers` (scaffold + register + bootstrap a new ticker into a
category by copying a sibling's calibration) and
`GET /super/tickers/{id}/status`,
`POST /super/regenerate` plus `GET /super/regenerate/status` — the launch is
detached, so the status endpoint checks the recorded PIDs against the live
process table (matched on **cmdline**, since a recycled PID would otherwise
look like a job still running, or worse, like ours finishing).

Changing the race target auto-invalidates the score cache
(`engine_scores.json` is fingerprinted on tp/sl) but does not re-discover
`ensemble.csv` candidates; changing the gates invalidates nothing, since they
filter scores that already exist. Both endpoints say so in their response.

**0DTE dealer gamma** — `GET /super/gex0dte` (stored view plus `stale`,
`window_open` and `pusher_state`), `POST /super/gex0dte/refresh`,
`POST /super/gex0dte/heartbeat` (one append-only row per push cycle, pass or
fail — the only thing that distinguishes a dead pusher from a blocked one),
`GET /super/gex0dte/history[?date=]` and `/history/dates` (hourly net gamma,
08:00–16:00 CST, uncaptured hours reported as 0 with `captured: false`).
See `docs/GEX_0DTE.md` — including why the server cannot fetch getgamma
itself, and the narrow Private Network Access allowance in `app/main.py` that
the browser pusher depends on.

## Safety model

- `VIDURA_PAPER_ONLY=true` (default): every bot start forces
  `DRY_RUN_MODE=TRUE`, `BOT152_DRY_RUN=TRUE`, `MAIN_PAPER=TRUE`; live mode is
  refused at the API. Flip deliberately, never by accident.
- `HALT_MACHINE_SHUTDOWN=FALSE` is always forced (legacy bots default to
  powering off the machine on halt).
- One running instance per (user, bot) — double-launch corrupted a live
  ledger once; the API returns 409 instead.
- Bot subprocesses never inherit `KALSHI_*` vars from the API process; each
  bot family loads credentials from its user's folder using its own contract
  (btc15: CWD; btc60: `BTC_CUSTOMER*` env; sports: argv + `SPORTS_*` env).
- The Kalshi endpoint in this API is read-only (balance/status/positions/
  fills/settlements); order placement stays inside the bots.
- The GEX endpoints never call the flashAlpha API (free tier: 5 requests/
  day, 2 of which the 09:00 CST scheduled job uses) — they only read the
  JSON files that job produces.

## Known data caveats (inherited)

- Legacy sports CSV `realized_pnl` before 2026-07-16 is overstated
  ("phantom"); true P&L comes from Kalshi fills+settlements. Ingest stores
  rows as recorded and keeps the original row in `trades.raw`.
- The btc60 paper CSV header is 21 columns while newer rows carry 22
  (`net30_at_entry` added later); the ingester tolerates the ragged column.
- `import bot_kalshi_btc15` is broken at the source-repo head (file renamed
  to `v4_bot_kalshi_btc15.py`); `bot_launcher.py` installs a lazy import
  alias so sports bots and btc15 v2/v3 still start.
