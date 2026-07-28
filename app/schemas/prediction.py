from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TennisModelInfo(BaseModel):
    model_id: str
    version: str
    name: str
    description: str
    target_markets: list[str]
    metrics: dict[str, float | str | int]
    script: str
    exists: bool


class TennisPredictionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: str
    match_name: str
    market_ticker: str | None
    pick: str | None
    probability: float | None
    confidence: str | None
    raw: dict | None
    created_at: datetime


class TennisPredictionIn(BaseModel):
    match_name: str
    market_ticker: str | None = None
    pick: str | None = None
    probability: float | None = None
    confidence: str | None = None
    raw: dict | None = None
