from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BotKey = Literal["btc15", "btc60", "sports", "manual"]


class TradeCreate(BaseModel):
    bot_key: BotKey = "manual"
    bot_version: str | None = None
    external_id: str | None = None
    ticker: str = Field(min_length=1, max_length=128)
    market_title: str | None = None
    side: Literal["yes", "no"] | None = None
    action: Literal["buy", "sell"] = "buy"
    contracts: int = Field(ge=0, default=1)
    price_cents: float | None = Field(default=None, ge=0, le=100)
    cost_usd: float | None = None
    fees_usd: float | None = None
    pnl_usd: float | None = None
    status: str = "open"
    is_mock: bool = True
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    raw: dict | None = None


class TradeOut(TradeCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    opened_at: datetime
    created_at: datetime


class TradeHistoryPage(BaseModel):
    total: int
    items: list[TradeOut]


class PerformanceSummary(BaseModel):
    bot_key: str | None
    trades: int
    settled: int
    wins: int
    losses: int
    win_rate: float | None
    total_cost_usd: float
    total_pnl_usd: float
    by_status: dict[str, int]
