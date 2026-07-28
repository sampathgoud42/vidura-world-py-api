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

    # --- source repo the bots live in ----------------------------------
    source_repo: Path = Path("D:/_projects/38trades-py-claude")
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

    @property
    def super_dir(self) -> Path:
        return self.source_repo / "super_research"

    # --- runtime dirs ---------------------------------------------------
    var_dir: Path = Path(__file__).resolve().parents[2] / "var"

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
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings
