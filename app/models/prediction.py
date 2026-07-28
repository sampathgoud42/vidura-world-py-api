from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.user import utcnow


class TennisPrediction(Base):
    """Stored output of a tennis prediction model run."""

    __tablename__ = "tennis_predictions"
    __table_args__ = (Index("ix_tennis_pred_model_time", "model_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(32))  # e.g. v5
    match_name: Mapped[str] = mapped_column(String(256))
    market_ticker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pick: Mapped[str | None] = mapped_column(String(128), nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
