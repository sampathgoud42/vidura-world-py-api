from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_user_or_404
from app.models import User
from app.schemas.kalshi import KalshiClientState
from app.services import credentials as creds_svc
from app.services.kalshi_client import KalshiApiError, KalshiAuthError, KalshiClient

router = APIRouter(prefix="/users/{user_id}", tags=["kalshi"])


class KalshiClientRequest(BaseModel):
    # Optional passphrase for password-protected PEM files.
    pem_password: str | None = None


@router.post(
    "/kalshi-client",
    operation_id="getKalshiClient",
    response_model=KalshiClientState,
)
def get_kalshi_client(
    payload: KalshiClientRequest | None = None,
    user: User = Depends(get_user_or_404),
) -> KalshiClientState:
    """Read credentials from the user's root folder, authenticate against
    Kalshi (read-only balance + exchange status), and return connection state.
    Never places orders."""
    try:
        creds = creds_svc.load_kalshi_credentials(user.user_root_folder)
    except creds_svc.CredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_424_FAILED_DEPENDENCY, detail=str(exc))

    pem_password = payload.pem_password if payload else None
    try:
        client = KalshiClient(
            creds.api_key_id,
            creds.private_key_path,
            creds.base_uri,
            pem_password=pem_password,
        )
    except KalshiAuthError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    checked_at = datetime.now(timezone.utc)
    try:
        balance = client.balance_cents()
        exchange = client.exchange_status()
        return KalshiClientState(
            user_id=user.user_id,
            environment="prod" if "external-api" in creds.base_uri else "custom",
            api_key_id_masked=creds.api_key_id_masked,
            authenticated=True,
            balance_cents=balance,
            exchange_active=exchange.get("exchange_active"),
            trading_active=exchange.get("trading_active"),
            checked_at=checked_at,
            detail=f"credentials from {creds.env_file.name}; dry_run={creds.dry_run}",
        )
    except KalshiApiError as exc:
        if exc.status_code in (401, 403):
            return KalshiClientState(
                user_id=user.user_id,
                environment="prod" if "external-api" in creds.base_uri else "custom",
                api_key_id_masked=creds.api_key_id_masked,
                authenticated=False,
                checked_at=checked_at,
                detail=f"Kalshi rejected credentials: HTTP {exc.status_code}",
            )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:  # network failures
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Kalshi unreachable: {exc}")
    finally:
        client.close()


class PasswordCheck(BaseModel):
    password: str


@router.post("/verify-password", operation_id="verifyUserPassword")
def verify_password(payload: PasswordCheck, user: User = Depends(get_user_or_404)) -> dict:
    """Timing-safe check against the folder's .sam password file (legacy
    wellness/sports app login contract)."""
    ok = creds_svc.verify_password(user.user_root_folder, payload.password)
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return {"user_id": user.user_id, "verified": True}
