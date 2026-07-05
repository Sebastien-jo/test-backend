"""Authentication endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User

log = get_logger("auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Hash of a throwaway password, verified against when the email is unknown so both
# paths cost one Argon2 verification — no user enumeration via response timing.
_DUMMY_HASH = hash_password("timing-equalization-placeholder")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    user_id: uuid.UUID
    organization_id: uuid.UUID


@router.post("/login", response_model=TokenResponse)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """Exchange email + password for a JWT access token.

    OAuth2PasswordRequestForm uses `username` for the form field; we treat it as
    the email.
    """
    result = await db.execute(select(User).where(User.email == form.username))
    user = result.scalar_one_or_none()

    # Same 401 whether the email is unknown or the password is wrong.
    hashed = user.hashed_password if user is not None else _DUMMY_HASH
    password_ok = verify_password(form.password, hashed)
    if user is None or not password_ok:
        # Email only — never the password/hash — and only on the failure path.
        log.warning("login failed", email=form.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.id, organization_id=user.organization_id)
    log.info("login succeeded", user_id=str(user.id), organization_id=str(user.organization_id))
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUserDep) -> MeResponse:
    """Return the authenticated identity — the simplest protected endpoint.

    Also what makes Swagger render the Authorize button (it registers the
    security scheme), so the login -> Authorize -> protected-call flow is
    testable end-to-end in /docs.
    """
    return MeResponse(
        user_id=current_user.user_id,
        organization_id=current_user.organization_id,
    )
