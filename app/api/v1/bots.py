"""Bot control endpoints.

Literal spec paths (/bots/btc/*, /bots/sports/*) plus a shared core.  For
the BTC endpoints, ``bot`` selects btc15 (default) or btc60 since both live
under the /bots/btc umbrella.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import User
from app.schemas.bot import (
    BotInfo,
    BotLogsOut,
    BotRunOut,
    BotStartRequest,
    BotStatusOut,
    BotStopRequest,
)
from app.schemas.trade import PerformanceSummary, TradeHistoryPage, TradeOut
from app.services import bot_manager, ingest
from app.services import trades as trades_svc
from app.services.bot_registry import BOTS, get_bot, registry_report

router = APIRouter(prefix="/bots", tags=["bots"])

BTC_KEYS = ("btc15", "btc60")


def _user_or_404(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return user


def _btc_key(bot: str) -> str:
    if bot not in BTC_KEYS:
        raise HTTPException(status_code=422, detail=f"bot must be one of {BTC_KEYS}")
    return bot


@router.get("", operation_id="getBots", response_model=list[BotInfo])
def get_bots(db: Session = Depends(get_db)) -> list[BotInfo]:
    out = []
    for item in registry_report():
        runs = bot_manager.reconcile_runs(db, bot_key=item["key"])
        active = next((r for r in runs if r.status == "running"), None)
        out.append(
            BotInfo(
                **item,
                running=active is not None,
                active_run_id=active.id if active else None,
            )
        )
    return out


# --- shared handlers -------------------------------------------------------

def _status(db: Session, bot_key: str, user_id: str | None) -> BotStatusOut:
    runs = bot_manager.reconcile_runs(db, bot_key=bot_key, user_id=user_id)
    return BotStatusOut(
        bot_key=bot_key,
        running=any(r.status == "running" for r in runs),
        runs=[BotRunOut.model_validate(r) for r in runs[:20]],
    )


def _start(db: Session, bot_key: str, payload: BotStartRequest) -> BotRunOut:
    user = _user_or_404(db, payload.user_id)
    try:
        run = bot_manager.start_bot(
            db, user, bot_key, version=payload.version, mode=payload.mode
        )
    except bot_manager.BotManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return BotRunOut.model_validate(run)


def _stop(db: Session, bot_key: str, payload: BotStopRequest) -> list[BotRunOut]:
    try:
        stopped = bot_manager.stop_bot(
            db, bot_key, user_id=payload.user_id, run_id=payload.run_id
        )
    except bot_manager.BotManagerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return [BotRunOut.model_validate(r) for r in stopped]


def _logs(db: Session, bot_key: str, user_id: str | None, run_id: int | None, lines: int) -> BotLogsOut:
    runs = bot_manager.reconcile_runs(db, bot_key=bot_key, user_id=user_id)
    if run_id is not None:
        runs = [r for r in runs if r.id == run_id]
    if not runs:
        raise HTTPException(status_code=404, detail=f"No {bot_key} runs found")
    run = runs[0]
    return BotLogsOut(
        bot_key=bot_key,
        run_id=run.id,
        log_file=run.log_file,
        lines=bot_manager.tail_log(run, lines),
    )


# --- BTC endpoints ---------------------------------------------------------

@router.get("/btc/status", operation_id="getBtcBotStatus", response_model=list[BotStatusOut])
def btc_status(
    user_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[BotStatusOut]:
    return [_status(db, key, user_id) for key in BTC_KEYS]


@router.post("/btc/start", operation_id="startBtcBot", response_model=BotRunOut, status_code=status.HTTP_201_CREATED)
def btc_start(
    payload: BotStartRequest,
    bot: str = Query(default="btc15", description="btc15 or btc60"),
    db: Session = Depends(get_db),
) -> BotRunOut:
    return _start(db, _btc_key(bot), payload)


@router.post("/btc/stop", operation_id="stopBtcBot", response_model=list[BotRunOut])
def btc_stop(
    payload: BotStopRequest,
    bot: str = Query(default="btc15", description="btc15 or btc60"),
    db: Session = Depends(get_db),
) -> list[BotRunOut]:
    return _stop(db, _btc_key(bot), payload)


@router.get("/btc/logs", operation_id="getBtcBotLogs", response_model=BotLogsOut)
def btc_logs(
    bot: str = Query(default="btc15"),
    user_id: str | None = Query(default=None),
    run_id: int | None = Query(default=None),
    lines: int = Query(default=100, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> BotLogsOut:
    return _logs(db, _btc_key(bot), user_id, run_id, lines)


@router.get("/btc/trades", operation_id="getBtcTrades", response_model=TradeHistoryPage)
def btc_trades(
    user_id: str | None = Query(default=None),
    bot: str | None = Query(default=None, description="filter: btc15 or btc60"),
    days: int | None = Query(default=None, ge=1, le=3660),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TradeHistoryPage:
    keys = [_btc_key(bot)] if bot else list(BTC_KEYS)
    total, items = trades_svc.query_trades(
        db, user_id=user_id, bot_key=keys, days=days, limit=limit, offset=offset
    )
    return TradeHistoryPage(total=total, items=[TradeOut.model_validate(t) for t in items])


@router.post("/btc/sync-trades", operation_id="syncBtcTrades")
def btc_sync_trades(
    user_id: str = Query(...),
    bot: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    user = _user_or_404(db, user_id)
    keys = [_btc_key(bot)] if bot else list(BTC_KEYS)
    return [ingest.sync_trades(db, user, key) for key in keys]


# --- sports endpoints ------------------------------------------------------

@router.get("/sports/config", operation_id="getSportsBotConfig")
def sports_config() -> dict:
    """Non-secret sports engine configuration (from the tracked env file —
    note the load-bearing 'kaslhi' filename typo in the source repo)."""
    settings = get_settings()
    sports_dir = settings.source_repo / "prediction-trade/kalshi/sports"
    config: dict[str, str] = {}
    for name in ("kaslhi_sports.env", "kalshi_sports.env"):
        path = sports_dir / name
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                # Belt and braces: never expose a secret-looking key.
                if any(s in key.upper() for s in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                    continue
                config.setdefault(key, value.strip())
            break
    spec = get_bot("sports")
    return {
        "bot": spec.name,
        "versions": [v.version for v in spec.versions],
        "config": config,
        "paper_only_server": settings.paper_only,
    }


@router.get("/sports/status", operation_id="getSportsBotStatus", response_model=BotStatusOut)
def sports_status(
    user_id: str | None = Query(default=None), db: Session = Depends(get_db)
) -> BotStatusOut:
    return _status(db, "sports", user_id)


@router.post("/sports/start", operation_id="startSportsBot", response_model=BotRunOut, status_code=status.HTTP_201_CREATED)
def sports_start(payload: BotStartRequest, db: Session = Depends(get_db)) -> BotRunOut:
    return _start(db, "sports", payload)


@router.post("/sports/stop", operation_id="stopSportsBot", response_model=list[BotRunOut])
def sports_stop(payload: BotStopRequest, db: Session = Depends(get_db)) -> list[BotRunOut]:
    return _stop(db, "sports", payload)


@router.get("/sports/logs", operation_id="getSportsBotLogs", response_model=BotLogsOut)
def sports_logs(
    user_id: str | None = Query(default=None),
    run_id: int | None = Query(default=None),
    lines: int = Query(default=100, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> BotLogsOut:
    return _logs(db, "sports", user_id, run_id, lines)


@router.get("/sports/active-bets", operation_id="getSportsActiveBets", response_model=list[TradeOut])
def sports_active_bets(
    user_id: str | None = Query(default=None), db: Session = Depends(get_db)
) -> list[TradeOut]:
    open_trades = trades_svc.active_trades(db, user_id=user_id, bot_key="sports")
    return [TradeOut.model_validate(t) for t in open_trades]


@router.get("/sports/performance", operation_id="getSportsPerformance", response_model=PerformanceSummary)
def sports_performance(
    user_id: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=3660),
    db: Session = Depends(get_db),
) -> PerformanceSummary:
    return trades_svc.performance(db, user_id=user_id, bot_key="sports", days=days)


@router.get("/sports/trades", operation_id="getSportsTrades", response_model=TradeHistoryPage)
def sports_trades(
    user_id: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=3660),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TradeHistoryPage:
    total, items = trades_svc.query_trades(
        db, user_id=user_id, bot_key="sports", days=days, limit=limit, offset=offset
    )
    return TradeHistoryPage(total=total, items=[TradeOut.model_validate(t) for t in items])


@router.post("/sports/sync-trades", operation_id="syncSportsTrades")
def sports_sync_trades(user_id: str = Query(...), db: Session = Depends(get_db)) -> dict:
    user = _user_or_404(db, user_id)
    return ingest.sync_trades(db, user, "sports")
