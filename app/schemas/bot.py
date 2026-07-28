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


class BotStartRequest(BaseModel):
    user_id: str
    version: str | None = Field(default=None, examples=["v2"])
    mode: str = Field(default="paper", pattern="^(paper|mock)$")


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
