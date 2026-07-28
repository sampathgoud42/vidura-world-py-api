# Vidura World API — minimal production image for Render / Google Cloud Run.
#
#   docker build -t vidura-api .
#   docker run -p 8790:8790 -e PORT=8790 vidura-api
#
# Two stages so build-only wheels never reach the runtime layer; the final
# image is python:3.11-slim + site-packages + app (no compilers, no cache).

# ---------- build ----------
FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Only the cloud dependency set: no yfinance/pandas/numpy/aiohttp (those are
# for the local bot runtime, which does not exist in a container).
COPY requirements-cloud.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements-cloud.txt

# ---------- runtime ----------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # cloud profile: DB-backed read API only, no bot subprocesses
    VIDURA_CLOUD_MODE=true \
    VIDURA_SUPER_AUTO_SYNC=false \
    # writable SQLite location inside the container (override with
    # VIDURA_DATABASE_URL to use Postgres, or mount a disk here)
    VIDURA_DATABASE_PATH=/data/app.db \
    VIDURA_VAR_DIR=/data/var

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY app ./app

# Non-root, with a writable data dir for the SQLite file / logs.
RUN useradd --create-home --uid 10001 vidura && \
    mkdir -p /data/var/logs && chown -R vidura:vidura /data /app
USER vidura

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8790\")}/health',timeout=4)" || exit 1

# Render and Cloud Run both inject $PORT; default keeps local runs working.
# One worker on purpose: in-process caches/locks are per-process state.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8790} --workers 1 --timeout-keep-alive 15"]
