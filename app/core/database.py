"""SQLAlchemy engine/session wiring for the local SQLite database."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        # WAL keeps readers (dashboards) from blocking bot-driven writers.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (idempotent). Called on app startup."""
    # Import models so they register on Base.metadata before create_all.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate(engine)


def _migrate(bind) -> None:
    """Tiny additive migrations — create_all never alters existing tables."""
    from sqlalchemy import text

    with bind.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(super_signals)")).fetchall()
        }
        if cols and "archived" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE super_signals "
                    "ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0"
                )
            )
