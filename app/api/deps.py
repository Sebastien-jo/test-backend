"""Shared API dependencies — authentication and the tenant context.

Every protected endpoint depends on `get_current_user`; the rest of the app
builds on it, so its signature is deliberately stable and minimal.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import InvalidTokenError, decode_access_token
from app.models import User

# tokenUrl must match the login route so Swagger's Authorize dialog can log in.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: uuid.UUID
    organization_id: uuid.UUID


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """Resolve the authenticated user from the bearer token.

    Multi-tenant invariant: the organization_id ALWAYS comes from the signed
    token, never from a client-supplied parameter. This is what keeps one
    organization's data invisible to another — do not add an org query param.
    """
    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise _credentials_error from None

    # The token could outlive the user (deleted/deactivated), so re-check the DB.
    user = await db.get(User, payload.user_id)
    if user is None:
        raise _credentials_error

    return CurrentUser(
        user_id=payload.user_id,
        organization_id=payload.organization_id,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
