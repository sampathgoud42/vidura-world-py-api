from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_user_or_404
from app.core import paths
from app.core.database import get_db
from app.models import User
from app.schemas.user import UserCreate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


def _to_out(user: User) -> UserOut:
    exists, _ = paths.validate_root_exists(user.user_root_folder)
    out = UserOut.model_validate(user)
    out.root_folder_exists = exists
    return out


@router.get("", operation_id="getUsers", response_model=list[UserOut])
def get_users(db: Session = Depends(get_db)) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.created_at)).all()
    return [_to_out(u) for u in users]


@router.post(
    "",
    operation_id="createUser",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    dupe = db.scalar(select(User).where(User.username == payload.username))
    if dupe is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{payload.username}' already exists",
        )
    if payload.email is not None:
        dupe = db.scalar(select(User).where(User.email == payload.email))
        if dupe is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Email '{payload.email}' already exists",
            )
    user = User(
        username=payload.username,
        email=payload.email,
        user_root_folder=payload.user_root_folder,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _to_out(user)


@router.get("/{user_id}", operation_id="getUser", response_model=UserOut)
def get_user(user: User = Depends(get_user_or_404)) -> UserOut:
    return _to_out(user)
