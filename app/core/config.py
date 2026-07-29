"""Application settings.

Every value can be overridden with a ``VIDURA_``-prefixed environment
variable, e.g. ``VIDURA_DATABASE_PATH=/home/app/data/app.db`` on Linux.
Defaults target the Windows workstation this project was born on.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDURA_", env_file=".env", extra="ignore")

    app_name: str = "Vidura World API"
    app_version: str = "1.0.0"
    api_v1_prefix: str = "/api/v1"

    # --- persistence ---------------------------------------------------
    database_path: Path = Path("D:/_projects/database/app.db")
    # Full SQLAlchemy URL override — set this to run on Postgres in the
    # cloud (e.g. Render/Neon: postgresql+psycopg://user:pw@host/db).
    # Empty = use the SQLite file above.
    database_url_override: str = ""

    # --- cloud profile ---------------------------------------------------
    # True on Render / Cloud Run: there is no bot repo and no place to spawn
    # long-running trading processes, so execution endpoints answer 503 and
    # only the DB-backed read APIs are served. Auto-enabled when the
    # platform sets PORT (Render/Cloud Run both do) unless set explicitly.
    cloud_mode: bool = False

    # --- trading runtime (vendored) --------------------------------------
    # The bot scripts, signal engines, prediction models and their config
    # live INSIDE this project under runtime/ — nothing is read from the
    # original 38trades-py-claude checkout. Override only if you keep the
    # runtime somewhere else.
    source_repo: Path = Path(__file__).resolve().parents[2] / "runtime"
    customers_root: Path = Path("D:/_projects/customers")

    # Python used to launch bot subprocesses (defaults to this app's venv
    # python; bots only need requests/cryptography which are installed here).
    bot_python: Path | None = None

    # Python for super_research supervisors/workers. They need yfinance and
    # friends, which live in the system install that already runs them daily
    # (schtask + bots.py), not in this venv.
    super_python: Path = Path(
        "C:/Users/sampa/AppData/Local/Python/pythoncore-3.14-64/python.exe"
    )

    # Background ingest: continuously mirror every signal the super_research
    # service generates (central ledgers + per-ticker worker CSVs + gex/econ
    # snapshots) into SQLite. Interval matches the supervisors' 60s poll.
    super_auto_sync: bool = True
    super_sync_interval: int = 60

    @property
    def super_dir(self) -> Path:
        return self.source_repo / "super_research"

    # --- runtime dirs ---------------------------------------------------
    var_dir: Path = Path(__file__).resolve().parents[2] / "var"

    # --- flashAlpha GEX ---------------------------------------------------
    # FREE plan = 5 requests/day TOTAL. The API is now the ONLY fetcher (the
    # FlashAlphaGEX_Daily Windows task was retired 2026-07-28 in favour of
    # the in-process 09:00 CST loop), so it owns the whole budget. Every call
    # is counted in the DB and refused past this cap. Lower it back to 3 if
    # you ever re-create that scheduled task.
    flashalpha_daily_cap: int = 5
    # Daily 09:00 CST snapshot inside the API — the replacement for the
    # FlashAlphaGEX_Daily Windows task. Disable if that task still exists,
    # or both will spend quota.
    gex_daily_enabled: bool = True
    flashalpha_api_key: str = ""  # else read from <source_repo>/super_research/flashalpha.env
    gex_tickers: str = "spy,qqq"

    # --- earnings calendar -------------------------------------------------
    # Keyless (yfinance), so no budget to ration — but a sweep is ~100 HTTP
    # calls, so a background loop keeps the cache warm and requests only ever
    # read it. Disable in tests / air-gapped hosts.
    earnings_enabled: bool = True

    # --- safety ---------------------------------------------------------
    # When True (default) bots are always launched in paper/mock mode and
    # order-placing endpoints record trades locally instead of hitting the
    # exchange.  Flip to False deliberately, never by accident.
    paper_only: bool = True

    # Optional shared API key. When set, every /api request must carry it in
    # the X-API-Key header. Empty (default) = open, for localhost/LAN dev.
    api_key: str = ""

    # By default user_root_folder must live under customers_root so the API
    # cannot be pointed at arbitrary filesystem folders holding secrets.
    allow_any_root: bool = False

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            # Render hands out legacy 'postgres://' URLs; SQLAlchemy 2 needs
            # an explicit driver.
            url = self.database_url_override
            if url.startswith("postgres://"):
                url = "postgresql+psycopg://" + url[len("postgres://"):]
            return url
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    import os

    settings = Settings()
    # PORT is injected by Render and Cloud Run; treat that as "cloud" unless
    # the operator said otherwise.
    if "VIDURA_CLOUD_MODE" not in os.environ and os.environ.get("PORT"):
        settings.cloud_mode = True
    if settings.cloud_mode:
        # Never auto-ingest from a bot repo that does not exist in a
        # container, and never try to run engines there.
        settings.super_auto_sync = False
    if settings.is_sqlite:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # read-only container filesystem
    return settings
