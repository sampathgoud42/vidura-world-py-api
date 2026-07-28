from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WellnessProfileIn(BaseModel):
    """Mirrors wellness-profile.json in the user root folder."""

    gender: str | None = None
    age: str | None = None
    ethnicity: str | None = None
    diet: str | None = None
    style: str | None = None
    goals: list[str] = Field(default_factory=list)
    region: str | None = None
    notifications: bool = True


class WellnessProfileOut(WellnessProfileIn):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    updated_at: datetime


class WellnessEntryIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64, examples=["checkin", "metric"])
    payload: dict = Field(default_factory=dict)
    recorded_at: datetime | None = None


class WellnessEntryOut(WellnessEntryIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    recorded_at: datetime
