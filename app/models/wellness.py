from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.user import utcnow


class WellnessProfile(Base):
    """Current wellness preferences (mirror of wellness-profile.json)."""

    __tablename__ = "wellness_profiles"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    age: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ethnicity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diet: Mapped[str | None] = mapped_column(String(64), nullable=True)
    style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    goals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user = relationship("User", back_populates="wellness_profile")


class WellnessEntry(Base):
    """Time-stamped wellness data points, queryable for the past N days."""

    __tablename__ = "wellness_entries"
    __table_args__ = (Index("ix_wellness_user_time", "user_id", "recorded_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))  # e.g. checkin, metric, profile_update
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
