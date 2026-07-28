# Vidura World API

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

Then open:

| URL | What |
| --- | --- |
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
| `daily_snapshots` | daily GEX / econ JSON history (`gex`, `econ`, `gex_raw_<ticker>`) |

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
