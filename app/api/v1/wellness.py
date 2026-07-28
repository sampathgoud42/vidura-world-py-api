from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_user_or_404
from app.core.database import get_db
from app.models import User, WellnessEntry
from app.schemas.wellness import (
    WellnessEntryIn,
    WellnessEntryOut,
    WellnessProfileIn,
    WellnessProfileOut,
)
from app.services import wellness as svc

router = APIRouter(prefix="/users/{user_id}/wellness", tags=["wellness"])

# Choices offered by the wellness app's selection screens.  Kept server-side
# so mobile/web clients stay in sync without an app release.
SELECTION_OPTIONS: dict[str, list[str]] = {
    "gender": ["Male", "Female", "Other"],
    "age": ["18-25", "25-30", "30-35", "35-40", "40-50", "50+"],
    "diet": ["Vegetarian", "Non-Vegetarian", "Vegan", "Eggetarian"],
    "style": ["Modern", "Traditional", "Both"],
    "goals": [
        "Cholesterol",
        "Fertility",
        "Weight Loss",
        "Muscle Gain",
        "Diabetes",
        "Heart Health",
        "Sleep",
        "Stress",
    ],
}


@router.get("/options", operation_id="getWellnessOptions")
def get_options(user: User = Depends(get_user_or_404)) -> dict[str, list[str]]:
    return SELECTION_OPTIONS


@router.get("/profile", operation_id="getWellnessProfile", response_model=WellnessProfileOut)
def get_profile(
    user: User = Depends(get_user_or_404), db: Session = Depends(get_db)
) -> WellnessProfileOut:
    profile = svc.get_or_import_profile(db, user)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No wellness profile in DB or user folder",
        )
    return WellnessProfileOut.model_validate(profile)


@router.put("/profile", operation_id="putWellnessProfile", response_model=WellnessProfileOut)
def put_profile(
    payload: WellnessProfileIn,
    user: User = Depends(get_user_or_404),
    db: Session = Depends(get_db),
) -> WellnessProfileOut:
    profile = svc.upsert_profile(db, user, payload)
    return WellnessProfileOut.model_validate(profile)


@router.get("/data", operation_id="getWellnessData", response_model=list[WellnessEntryOut])
def get_data(
    days: int = Query(default=60, ge=1, le=366),
    kind: str | None = Query(default=None),
    user: User = Depends(get_user_or_404),
    db: Session = Depends(get_db),
) -> list[WellnessEntryOut]:
    entries = svc.entries_since(db, user, days=days, kind=kind)
    return [WellnessEntryOut.model_validate(e) for e in entries]


@router.post(
    "/data",
    operation_id="addWellnessData",
    response_model=WellnessEntryOut,
    status_code=status.HTTP_201_CREATED,
)
def add_data(
    payload: WellnessEntryIn,
    user: User = Depends(get_user_or_404),
    db: Session = Depends(get_db),
) -> WellnessEntryOut:
    entry = WellnessEntry(
        user_id=user.user_id,
        kind=payload.kind,
        payload=payload.payload,
    )
    if payload.recorded_at is not None:
        entry.recorded_at = payload.recorded_at
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return WellnessEntryOut.model_validate(entry)
