from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BotVersionInfo(BaseModel):
    version: str
    script: str
    exists: bool
    default: bool = False


class BotInfo(BaseModel):
    key: str
    name: str
    category: str  # btc | sports
    cadence: str | None = None
    versions: list[BotVersionInfo]
    running: bool
    active_run_id: int | None = None


class SportSettings(BaseModel):
    """Per-sport tuning for the main sports bot (maps to <SPORT>_* env)."""

    contracts: int | None = Field(default=None, ge=1, le=1000, description="contracts per order")
    bank: float | None = Field(default=None, ge=0, description="session bankroll $ (0 = unlimited)")
    model: str | None = Field(
        default=None,
        pattern="^v[1-5]$",
        description="prediction model version for this sport (tennis: v1-v5, "
        "default v5 — the forensics-validated whitelist)",
        examples=["v5"],
    )


class BotStartRequest(BaseModel):
    user_id: str
    version: str | None = Field(default=None, examples=["v2"])
    # 'paper' is always the default; 'live' additionally requires the server
    # to be unlocked with VIDURA_PAPER_ONLY=false (403 otherwise).
    mode: str = Field(default="paper", pattern="^(paper|mock|live)$")
    # --- sports-bot options (ignored by btc bots) ---
    sports: list[str] | None = Field(
        default=None,
        description="active sports for the main bot, e.g. ['tennis','baseball']",
        examples=[["tennis", "baseball"]],
    )
    sport_settings: dict[str, SportSettings] | None = Field(
        default=None,
        description="per-sport contracts/bank, keyed by sport name",
        examples=[{"tennis": {"contracts": 20, "bank": 300}}],
    )
    target_pct: float | None = Field(
        default=None, ge=0, le=1000,
        description="profit target % on the bankroll/portfolio (TARGET_PORTFOLIO_PCT); bot halts when reached",
    )
    kill_existing: bool = Field(
        default=False,
        description="kill any process already running this bot — including copies "
        "the API did not start (orphans, legacy scheduler, manual launches) — "
        "then start fresh",
    )
    contracts: int | None = Field(
        default=None, ge=1, le=1000,
        description="fixed contracts per order (BTC bots: KALSHI_CONTRACTS/BOT152_CONTRACTS; "
        "sports: applied to every selected sport unless sport_settings overrides it)",
    )


class BotStopRequest(BaseModel):
    user_id: str | None = None
    run_id: int | None = None


class BotRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    bot_key: str
    bot_version: str | None
    script_path: str | None
    pid: int | None
    mode: str
    status: str
    exit_code: int | None
    log_file: str | None
    extra: dict | None = None
    started_at: datetime
    stopped_at: datetime | None


class BotStatusOut(BaseModel):
    bot_key: str
    running: bool
    runs: list[BotRunOut]


class BotLogsOut(BaseModel):
    bot_key: str
    run_id: int | None
    log_file: str | None
    lines: list[str]
