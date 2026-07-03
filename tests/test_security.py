"""Unit tests for password hashing and JWT handling (infra-free)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.security import (
    InvalidTokenError,
    TokenPayload,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_password() -> None:
    hashed = hash_password("s3cret-password")
    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_with_malformed_hash_returns_false() -> None:
    assert verify_password("whatever", "not-a-real-argon2-hash") is False


def test_token_roundtrip() -> None:
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()

    payload = decode_access_token(create_access_token(user_id, organization_id))

    assert payload == TokenPayload(user_id=user_id, organization_id=organization_id)


def test_decode_expired_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_expires_minutes", -1)
    token = create_access_token(uuid.uuid4(), uuid.uuid4())

    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_decode_bad_signature_raises() -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "org": str(uuid.uuid4())},
        "a-completely-different-secret-of-adequate-length",
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(token)


def test_decode_missing_claims_raises() -> None:
    token = jwt.encode(
        {"exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(token)
