"""Cloud-mode guard.

On Render / Cloud Run there is no 38trades bot repo and no place to run
long-lived trading processes, so the endpoints that *execute* things are
refused with a clear 503 while every DB-backed read endpoint keeps working.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import get_settings


def require_local_runtime(what: str) -> None:
    """Raise 503 when running in the cloud profile."""
    if get_settings().cloud_mode:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{what} needs the local runtime (bot scripts + process control). "
                "This deployment runs in cloud mode and serves the read-only, "
                "database-backed API only."
            ),
        )
