from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.user import utcnow


class SuperSignal(Base):
    """One row per super_research signal (A-book / B-book ledgers and the
    per-ticker intraday feeds), ingested from the CSVs for queryable history."""

    __tablename__ = "super_signals"
    __table_args__ = (
        Index("ix_super_signals_book_time", "book", "logged_at"),
        Index("ix_super_signals_ticker_time", "ticker", "logged_at"),
        Index("ix_super_signals_external", "external_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(256))  # dedupe key from CSV row
    book: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A | B
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticker: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)  # LONG | SHORT
    grade: Mapped[str | None] = mapped_column(String(16), nullable=True)  # eng_hot 2/3/4 etc
    combo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bar_time: Mapped[str | None] = mapped_column(String(64), nullable=True)  # CST as recorded
    logged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailySnapshot(Base):
    """Daily JSON blobs (GEX walls/regime, econ calendar) kept as history.

    kind: 'gex' | 'econ'. One row per (kind, snapshot_date); the latest row
    is what /super/state embeds, older rows are queryable history.
    """

    __tablename__ = "daily_snapshots"
    __table_args__ = (Index("ix_daily_snapshots_kind_date", "kind", "snapshot_date", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    snapshot_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
