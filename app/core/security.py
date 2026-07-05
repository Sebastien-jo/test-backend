"""Password hashing and JWT access tokens.

No FastAPI/DB dependencies here — pure functions over strings and ids, so this
module is unit-testable in isolation.
"""

import hashlib
import hmac
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


def compute_partner_signature(body: bytes) -> str:
    """HMAC-SHA256 of the exact request body, hex-encoded, keyed on the shared secret."""
    return hmac.new(settings.partner_hmac_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_partner_signature(body: bytes, signature: str) -> bool:
    """Constant-time check of an inbound partner signature against the raw body.

    `body` MUST be the exact bytes received (never a re-serialized JSON) — any
    whitespace or key-order difference changes the HMAC. Returns False for a
    missing signature; uses `compare_digest` to avoid timing side channels.
    """
    if not signature:
        return False
    return hmac.compare_digest(compute_partner_signature(body), signature)


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
