from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Trade
from app.schemas.trade import PerformanceSummary, TradeCreate

WIN_STATUSES = {"won", "win"}
LOSS_STATUSES = {"lost", "loss"}
SETTLED_STATUSES = WIN_STATUSES | LOSS_STATUSES | {"settled", "closed"}


def record_trade(db: Session, user_id: str, payload: TradeCreate) -> Trade:
    data = payload.model_dump(exclude_none=True)
    trade = Trade(user_id=user_id, **data)
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def query_trades(
    db: Session,
    *,
    user_id: str | None = None,
    bot_key: str | None = None,
    bot_version: str | None = None,
    status: str | None = None,
    days: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[Trade]]:
    stmt = select(Trade)
    if user_id:
        stmt = stmt.where(Trade.user_id == user_id)
    if bot_key:
        stmt = stmt.where(Trade.bot_key == bot_key)
    if bot_version:
        stmt = stmt.where(Trade.bot_version == bot_version)
    if status:
        stmt = stmt.where(Trade.status == status)
    if days:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        stmt = stmt.where(Trade.opened_at >= cutoff)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(Trade.opened_at.desc()).limit(limit).offset(offset)
    return total, list(db.scalars(stmt).all())


def active_trades(db: Session, *, user_id: str | None = None, bot_key: str | None = None) -> list[Trade]:
    stmt = select(Trade).where(Trade.status == "open")
    if user_id:
        stmt = stmt.where(Trade.user_id == user_id)
    if bot_key:
        stmt = stmt.where(Trade.bot_key == bot_key)
    return list(db.scalars(stmt.order_by(Trade.opened_at.desc())).all())


def performance(
    db: Session, *, user_id: str | None = None, bot_key: str | None = None, days: int | None = None
) -> PerformanceSummary:
    _, trades = query_trades(
        db, user_id=user_id, bot_key=bot_key, days=days, limit=100_000, offset=0
    )
    by_status: dict[str, int] = {}
    wins = losses = settled = 0
    total_cost = total_pnl = 0.0
    for t in trades:
        by_status[t.status] = by_status.get(t.status, 0) + 1
        if t.status in WIN_STATUSES:
            wins += 1
        elif t.status in LOSS_STATUSES:
            losses += 1
        if t.status in SETTLED_STATUSES:
            settled += 1
        total_cost += t.cost_usd or 0.0
        total_pnl += t.pnl_usd or 0.0
    decided = wins + losses
    return PerformanceSummary(
        bot_key=bot_key,
        trades=len(trades),
        settled=settled,
        wins=wins,
        losses=losses,
        win_rate=round(wins / decided, 4) if decided else None,
        total_cost_usd=round(total_cost, 2),
        total_pnl_usd=round(total_pnl, 2),
        by_status=by_status,
    )
