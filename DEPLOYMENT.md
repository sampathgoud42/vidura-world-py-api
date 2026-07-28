# Vidura World — Deployment Guide (any host)

How to run the Vidura World stack anywhere: the **API** (FastAPI + SQLite,
this repo), the **web app** (`vidura-world-js`, Vite/React SPA), and the
**legacy bot repo** the API wraps (`38trades-py-claude`).

```
┌────────────────┐   HTTP :8790    ┌──────────────────┐   subprocess   ┌────────────────────┐
│ vidura-world-js│ ──────────────► │  vidura-world API │ ─────────────► │ 38trades-py-claude │
│  (static SPA)  │                 │ FastAPI + SQLite  │                │  bots + engines    │
└────────────────┘                 └──────────────────┘                └────────────────────┘
                                          │
                                          ▼
                                 D:\_projects\database\app.db   (or any path)
```

---

## 1. Prerequisites

| Component | Requirement |
| --- | --- |
| Python | 3.12+ (3.14 tested) |
| Node.js | 18+ (for building the web app) |
| Bot repo | a checkout of `38trades-py-claude` (only needed on the host that runs bots/engines) |
| User folders | one folder per user with `.env` (Kalshi creds), `*.pem`, `.sam`, `wellness-profile.json` |

The API itself is pure Python — no compilers, no external database.

## 2. Install the API

```bash
git clone <your-remote>/vidura-world && cd vidura-world     # or copy the folder
python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# Linux/macOS:
.venv/bin/pip install -r requirements.txt
```

## 3. Configure (environment variables)

Every setting has a `VIDURA_` env var (or put them in a `.env` file next to
`run.bat`). Defaults target the original Windows workstation; on any other
host set at least the paths:

| Variable | Default | Notes |
| --- | --- | --- |
| `VIDURA_DATABASE_PATH` | `D:/_projects/database/app.db` | SQLite file; parent dir auto-created |
| `VIDURA_SOURCE_REPO` | `D:/_projects/38trades-py-claude` | bot repo root (bots/engines/gex files) |
| `VIDURA_CUSTOMERS_ROOT` | `D:/_projects/customers` | user folders must live under this |
| `VIDURA_PAPER_ONLY` | `true` | keep `true` unless you deliberately go live |
| `VIDURA_API_KEY` | *(empty = open)* | set for any non-LAN deployment; clients send `X-API-Key` |
| `VIDURA_ALLOW_ANY_ROOT` | `false` | leave false in production |
| `VIDURA_BOT_PYTHON` | API venv python | interpreter for Kalshi bot subprocesses |
| `VIDURA_SUPER_PYTHON` | system python path | interpreter for super_research engines (needs `yfinance`) |
| `VIDURA_SUPER_AUTO_SYNC` | `true` | background signal→SQLite ingest loop |
| `VIDURA_SUPER_SYNC_INTERVAL` | `60` | seconds between ingest passes |

Linux example:

```bash
export VIDURA_DATABASE_PATH=/srv/vidura/data/app.db
export VIDURA_SOURCE_REPO=/srv/vidura/38trades-py-claude
export VIDURA_CUSTOMERS_ROOT=/srv/vidura/customers
export VIDURA_SUPER_PYTHON=/usr/bin/python3
export VIDURA_API_KEY=$(openssl rand -hex 24)
```

Path portability: `user_root_folder` values stored in the DB may be Windows
(`D:\...`) or POSIX (`/home/...`) form — the API normalizes both, so the
same DB file can move between OSes as long as the folders exist.

## 4. Run the API

Development / single host:

```bash
# Windows
run.bat
# Linux/macOS
./run.sh
```

Both run `uvicorn app.main:app --host 0.0.0.0 --port 8790`.

**Important: exactly ONE worker.** Bot process handles, supervisor liveness
caches, and the auto-sync loop are in-process state — do not use
`--workers N` or Gunicorn multi-worker. One uvicorn worker easily serves
LAN/mobile traffic for this workload.

### As a service — Windows (Task Scheduler)

```powershell
schtasks /Create /TN "ViduraWorldAPI" /SC ONSTART /RU SYSTEM ^
  /TR "D:\_projects\vidura-world\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8790 --app-dir D:\_projects\vidura-world"
schtasks /Run /TN "ViduraWorldAPI"
```

(or use [NSSM](https://nssm.cc): `nssm install ViduraWorldAPI ...` for a real
Windows service with restart-on-crash.)

### As a service — Linux (systemd)

`/etc/systemd/system/vidura-api.service`:

```ini
[Unit]
Description=Vidura World API
After=network.target

[Service]
User=vidura
WorkingDirectory=/srv/vidura/vidura-world
EnvironmentFile=/srv/vidura/vidura-world/.env
ExecStart=/srv/vidura/vidura-world/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8790
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vidura-api
```

### Reverse proxy (recommended for anything beyond LAN)

nginx:

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;
    # ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
    }
}
```

Set `VIDURA_API_KEY` and serve only via TLS when exposed to the internet.

## 5. Verify the API

```bash
curl http://localhost:8790/health
```

Expect `{"status":"ok", ..., "paper_only":true}`. Interactive docs at
`/docs` (Swagger) and `/redoc`; machine spec at `/openapi.json`. Then:

```bash
.venv/bin/python -m pytest                      # unit/E2E suite (temp DB)
.venv/bin/python scripts/smoke_live.py --help   # live smoke against a real user folder
```

## 6. Deploy the web app (vidura-world-js)

The SPA is static after build — host it anywhere (the API has permissive
CORS and header-based auth, no cookies).

```bash
cd vidura-world-js
npm install
npm run build            # -> dist/
```

Serve `dist/` with any static host:

- **Same host, simplest:** `npm run preview -- --host 0.0.0.0 --port 4173`
- **nginx:** `root /srv/vidura/vidura-world-js/dist; try_files $uri /index.html;`
- **Netlify/Vercel:** deploy `dist/` as-is.

**Pointing the app at the API:** the four Vidura worlds read the API base
URL from, in priority order: `?api=https://host:8790` URL param (persisted
to localStorage), the `VITE_VIDURA_API` build-time env var, else same-origin
`/api/v1` (for reverse-proxy setups that route `/api` to the API — see
nginx snippet below). No other configuration exists in the frontend — all
data comes from the API.

Single-domain nginx (SPA + API together):

```nginx
location /api/ { proxy_pass http://127.0.0.1:8790; }
location /     { root /srv/vidura/vidura-world-js/dist; try_files $uri /index.html; }
```

## 7. Bots and engines on the host

- **Kalshi bots** (btc15/btc60/sports) are started per-user through the API
  (`POST /api/v1/bots/...`). They run from `VIDURA_SOURCE_REPO` with
  credentials from each user's folder. `VIDURA_PAPER_ONLY=true` forces
  dry-run flags on every start.
- **super_research engines** run either via the API (`POST /super/on`,
  `POST /super/regenerate`) or via the legacy scheduled tasks — both are
  detected and reported identically. They need `VIDURA_SUPER_PYTHON` to
  have `yfinance`, `pandas`, `numpy` installed.
- **GEX**: the flashAlpha free tier allows 5 requests/day; the daily 09:00
  CST job in the bot repo owns that budget. The API only reads its output —
  keep that job scheduled on whichever host holds the bot repo.

## 8. Backup & migration

Everything durable lives in exactly two places:

1. the SQLite file (`VIDURA_DATABASE_PATH`) — copy it (plus `-wal`/`-shm`
   when the API is stopped, or use `sqlite3 app.db ".backup out.db"` hot);
2. the user folders under `VIDURA_CUSTOMERS_ROOT` (credentials, wellness
   profiles, trade CSVs).

Copy both to a new host, set the env vars, start the service — done.

## 9. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401 Missing or invalid X-API-Key` | client must send the `X-API-Key` header (or unset `VIDURA_API_KEY`) |
| Bot start returns 409 "already running" | one instance per user per bot is enforced; stop it first |
| Bot start returns 409 about DRY_RUN_MODE | the user's `.env` pins a live flag; remove it or set TRUE |
| Bot exits immediately (502 with log tail) | read the tail: usually missing deps in `VIDURA_BOT_PYTHON` or bad creds |
| `/super/state` categories all `live:false` | supervisors not running — `POST /super/on` or check `VIDURA_SUPER_PYTHON` |
| GEX shows `stale:true` | the daily flashAlpha job failed (quota/network); yesterday's data is retained |
| DB locked errors | ensure only ONE uvicorn worker; WAL mode + 5s busy timeout are already set |
