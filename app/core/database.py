"""SQLAlchemy engine/session wiring for the local SQLite database."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        # check_same_thread is a SQLite-only DBAPI flag
        connect_args={"check_same_thread": False} if is_sqlite else {},
        pool_pre_ping=True,
    )
    if not is_sqlite:
        return engine

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
    """Tiny additive migrations — create_all never alters existing tables.

    SQLite only: on Postgres, create_all already builds the current schema
    (there is no pre-existing legacy database to patch)."""
    from sqlalchemy import text

    if bind.dialect.name != "sqlite":
        return

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

        # trades.is_live: real money or not, for the ledger's LIVE column.
        # NULLABLE on purpose — btc15 v2/v3/v4 write paper and live rows to the
        # same CSV with no distinguishing field, so their mode is genuinely
        # unknown. Backfill from is_mock everywhere it IS known, and leave
        # those rows NULL rather than mislabelling real trades as paper.
        tcols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(trades)")).fetchall()
        }
        if tcols and "is_live" not in tcols:
            conn.execute(text("ALTER TABLE trades ADD COLUMN is_live BOOLEAN"))
            conn.execute(
                text(
                    "UPDATE trades SET is_live = NOT is_mock "
                    "WHERE NOT (bot_key = 'btc15' AND bot_version IN ('v2','v3','v4'))"
                )
            )
