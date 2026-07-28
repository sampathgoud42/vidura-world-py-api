from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core import paths


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: EmailStr | None = None
    user_root_folder: str = Field(
        description="Windows (D:\\...) or POSIX (/home/...) path to the user's "
        "secrets folder (API keys, PEM, wellness profile)",
        min_length=1,
        max_length=1024,
    )

    @field_validator("user_root_folder")
    @classmethod
    def _canonicalize(cls, v: str) -> str:
        # Normalizes either notation to forward-slash canonical form and
        # rejects strings that cannot be parsed as a path at all.
        return paths.canonical_str(v.strip())


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    username: str
    email: str | None
    user_root_folder: str
    root_folder_exists: bool | None = None
    created_at: datetime
    updated_at: datetime
