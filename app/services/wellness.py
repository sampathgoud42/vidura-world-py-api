"""Wellness profile sync between the user's root folder and SQLite.

The JSON file (``wellness-profile.json``) stays the source the mobile apps
already know; SQLite is the queryable mirror plus the 60-day activity log.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import paths
from app.models import User, WellnessEntry, WellnessProfile
from app.schemas.wellness import WellnessProfileIn

logger = logging.getLogger(__name__)

PROFILE_FILENAME = "wellness-profile.json"


def read_profile_file(user: User) -> dict | None:
    """Load wellness-profile.json from the user's root folder, if present."""
    try:
        profile_path = paths.resolve_user_file(user.user_root_folder, PROFILE_FILENAME)
    except (paths.PathSecurityError, ValueError) as exc:
        logger.warning("Bad root folder for user %s: %s", user.user_id, exc)
        return None
    if not profile_path.is_file():
        return None
    try:
        return json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable wellness profile for %s: %s", user.user_id, exc)
        return None


def write_profile_file(user: User, data: dict) -> bool:
    """Persist the profile back to the user folder (best-effort)."""
    try:
        profile_path = paths.resolve_user_file(user.user_root_folder, PROFILE_FILENAME)
        profile_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except (paths.PathSecurityError, ValueError, OSError) as exc:
        logger.warning("Could not write wellness profile for %s: %s", user.user_id, exc)
        return False


def upsert_profile(
    db: Session, user: User, payload: WellnessProfileIn, *, sync_file: bool = True
) -> WellnessProfile:
    profile = db.get(WellnessProfile, user.user_id)
    if profile is None:
        profile = WellnessProfile(user_id=user.user_id)
        db.add(profile)
    for field, value in payload.model_dump().items():
        setattr(profile, field, value)
    # Every preference change is also a time-series event so the apps can
    # show "what changed in the last 60 days".
    db.add(
        WellnessEntry(
            user_id=user.user_id,
            kind="profile_update",
            payload=payload.model_dump(),
        )
    )
    db.commit()
    db.refresh(profile)
    if sync_file:
        write_profile_file(user, payload.model_dump())
    return profile


def get_or_import_profile(db: Session, user: User) -> WellnessProfile | None:
    """Return the DB profile, importing from the JSON file on first touch."""
    profile = db.get(WellnessProfile, user.user_id)
    if profile is not None:
        return profile
    raw = read_profile_file(user)
    if raw is None:
        return None
    payload = WellnessProfileIn.model_validate(raw)
    return upsert_profile(db, user, payload, sync_file=False)


def entries_since(
    db: Session, user: User, *, days: int = 60, kind: str | None = None
) -> list[WellnessEntry]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    stmt = (
        select(WellnessEntry)
        .where(WellnessEntry.user_id == user.user_id, WellnessEntry.recorded_at >= cutoff)
        .order_by(WellnessEntry.recorded_at.desc())
    )
    if kind:
        stmt = stmt.where(WellnessEntry.kind == kind)
    return list(db.scalars(stmt).all())
