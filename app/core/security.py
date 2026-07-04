"""Password hashing and JWT access tokens.

No FastAPI/DB dependencies here — pure functions over strings and ids, so this
module is unit-testable in isolation.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.core.config import settings

# Argon2id is the current OWASP-recommended password hash: memory-hard (resistant
# to GPU/ASIC cracking) and, unlike bcrypt, with no silent 72-byte input limit.
_password_hasher = PasswordHasher()

_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return _password_hasher.verify(hashed_password, password)
    except Argon2Error, InvalidHashError:
        return False


class InvalidTokenError(Exception):
    """Raised when a token fails signature, expiry, or claim validation."""


@dataclass(frozen=True, slots=True)
class TokenPayload:
    user_id: uuid.UUID
    organization_id: uuid.UUID


def create_access_token(user_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "org": str(organization_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expires_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> TokenPayload:
    """Verify signature + expiry and return the typed payload.

    Raises `InvalidTokenError` on any problem (bad signature, expired, missing or
    malformed claims).
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    try:
        return TokenPayload(
            user_id=uuid.UUID(claims["sub"]),
            organization_id=uuid.UUID(claims["org"]),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("missing or malformed claims") from exc
