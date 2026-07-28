# Deploying the Vidura World API to the cloud (Render / Cloud Run)

The API ships a **cloud profile**: the database-backed read APIs (signals,
GEX/econ, trades, wellness, tennis models, desk state) run anywhere in a
container, while the endpoints that *execute* local things (starting bots,
regenerating engines) answer `503` with a clear message.

That split is what makes a free tier viable — those execution endpoints
need the bot scripts and long-lived processes, which no free container
platform will host anyway.

| Works in the cloud | Stays on your machine |
| --- | --- |
| `GET /super/state`, `/super/signals`, `/super/gex`, `/super/econ`, `/super/snapshots` | `POST /super/on`, `/super/off`, `/super/regenerate`, `/super/econ/refresh` |
| `GET/POST /users`, `/users/{id}/trades`, `/users/{id}/wellness/*` | `POST /bots/*/start`, `/bots/*/stop` |
| `GET /bots`, `/bots/*/status|logs|trades|performance|active-bets` | the bots and engines themselves |
| `GET /models/tennis`, `/models/tennis/{id}/predictions` | |

---

## 1. Files in this repo

| File | Purpose |
| --- | --- |
| `Dockerfile` | two-stage build on `python:3.11-slim`, non-root, `$PORT`-aware |
| `requirements-cloud.txt` | container deps only (no pandas/numpy/yfinance/psutil) |
| `.dockerignore` | keeps the build context to `app/` + requirements |
| `render.yaml` | Render Blueprint (free web service, health check, generated API key) |

## 2. Push to GitHub

```bash
cd D:\_projects\vidura-world
git remote add origin https://github.com/<you>/vidura-world.git
git push -u origin main
```

The `.gitignore` already excludes `.venv/`, `var/`, `*.db` and `.env`, so no
secrets or databases are pushed.

## 3a. Deploy on Render (easiest)

1. **New +** → **Blueprint** → pick the repo. Render reads `render.yaml`,
   builds the Dockerfile and starts the service on the free plan.
2. Copy the generated `VIDURA_API_KEY` from the service's *Environment* tab.
3. Open `https://<service>.onrender.com/docs`.

Manual alternative (no blueprint): **New +** → **Web Service** → *Docker* →
health check path `/health` → add the env vars from `render.yaml`.

**Free-tier realities:**
- the service **sleeps after ~15 min idle**; the next request takes ~30–50 s
  to wake it (fine for a dashboard, poor for a poller).
- the filesystem is **ephemeral** — a container restart wipes the SQLite
  file. Choose one:
  - *demo/read-only*: bake a snapshot into the image (`COPY app.db /data/app.db`);
  - *persistent*: uncomment the `databases:` block in `render.yaml` for free
    Postgres (expires after 30 days), or attach a paid disk at `/data`.

## 3b. Deploy on Google Cloud Run

```bash
gcloud run deploy vidura-api \
  --source . --region us-central1 --allow-unauthenticated \
  --memory 512Mi --cpu 1 --max-instances 1 \
  --set-env-vars VIDURA_CLOUD_MODE=true,VIDURA_SUPER_AUTO_SYNC=false,VIDURA_API_KEY=<key>
```

Keep `--max-instances 1`: the in-process caches and single-writer SQLite
assume one instance. Cloud Run's filesystem is ephemeral too — point
`VIDURA_DATABASE_URL` at Cloud SQL/Neon for durable data.

## 4. Environment variables

| Variable | Cloud value | Notes |
| --- | --- | --- |
| `PORT` | injected | the platform sets it; auto-enables cloud mode |
| `VIDURA_CLOUD_MODE` | `true` | 503s the execution endpoints |
| `VIDURA_SUPER_AUTO_SYNC` | `false` | no bot repo to ingest from |
| `VIDURA_API_KEY` | generated | clients send `X-API-Key` (GET `/api/super/state` stays open for the SPA) |
| `VIDURA_DATABASE_URL` | *(optional)* | `postgresql+psycopg://…`; `postgres://` is normalized automatically |
| `VIDURA_DATABASE_PATH` | `/data/app.db` | used when no `DATABASE_URL` |
| `VIDURA_PAPER_ONLY` | `true` | keep it true |

## 5. Getting your data into the cloud instance

The cloud instance never reads the bot repo, so feed it from your machine:

- **Postgres:** point your *local* API at the same `VIDURA_DATABASE_URL`
  and let its auto-sync loop write straight to the cloud database.
- **SQLite snapshot:** copy `D:\_projects\database\app.db` into the image
  (`COPY app.db /data/app.db`) or onto a mounted disk. Hot copies:
  `sqlite3 app.db ".backup out.db"`.

## 6. Point the web app at it

Build `vidura-world-js` and open it with the API base once — it persists:

```
https://<your-app>/?api=https://vidura-api.onrender.com
```

or set `VITE_VIDURA_API=https://vidura-api.onrender.com` before `npm run build`.

## 7. Local container test

```bash
docker build -t vidura-api .
docker run --rm -p 8790:8790 -e PORT=8790 -e VIDURA_API_KEY=dev vidura-api
curl http://localhost:8790/health
```

## 8. Verified so far

- App boots on the **cloud-only dependency set** (no psutil/pandas/yfinance):
  `/health`, `/users`, `/bots`, `/models/tennis`, `/super/state`,
  `/super/signals` all `200`; `/super/on` and `/bots/sports/start` `503`
  with the cloud-mode message; 41 OpenAPI paths.
- Cloud mode auto-detects from `PORT`; `VIDURA_DATABASE_URL` overrides and
  normalizes `postgres://` → `postgresql+psycopg://`.
- No Python 3.12+ syntax in `app/` (scanned), so the 3.11 base image is safe.
- **Not verified here:** the actual `docker build` — Docker is not installed
  on this machine. The Dockerfile follows the standard two-stage venv
  pattern; run step 7 once on a machine with Docker before deploying.
