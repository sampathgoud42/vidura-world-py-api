from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import TennisPrediction
from app.schemas.prediction import TennisModelInfo, TennisPredictionIn, TennisPredictionOut
from app.services import tennis_models

router = APIRouter(prefix="/models/tennis", tags=["tennis-models"])


@router.get("", operation_id="getTennisPredictionModels", response_model=list[TennisModelInfo])
def get_models() -> list[TennisModelInfo]:
    return [TennisModelInfo(**m) for m in tennis_models.list_models()]


def _model_or_404(model_id: str) -> dict:
    model = tennis_models.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Unknown tennis model '{model_id}'")
    return model


@router.get(
    "/{model_id}/predictions",
    operation_id="getTennisPredictions",
    response_model=list[TennisPredictionOut],
)
def get_predictions(
    model_id: str,
    hours: int = Query(default=48, ge=1, le=24 * 60, description="lookback window"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[TennisPredictionOut]:
    _model_or_404(model_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.scalars(
        select(TennisPrediction)
        .where(TennisPrediction.model_id == model_id, TennisPrediction.created_at >= cutoff)
        .order_by(TennisPrediction.created_at.desc())
        .limit(limit)
    ).all()
    return [TennisPredictionOut.model_validate(r) for r in rows]


@router.post(
    "/{model_id}/predictions",
    operation_id="recordTennisPrediction",
    response_model=TennisPredictionOut,
    status_code=status.HTTP_201_CREATED,
)
def record_prediction(
    model_id: str,
    payload: TennisPredictionIn,
    db: Session = Depends(get_db),
) -> TennisPredictionOut:
    """Record a prediction produced by a model run (bots and scripts post
    here so mobile/web clients can read a unified prediction feed)."""
    _model_or_404(model_id)
    row = TennisPrediction(model_id=model_id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return TennisPredictionOut.model_validate(row)
