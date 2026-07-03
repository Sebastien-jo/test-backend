"""Tests for the auth dependency and the login endpoint (infra-free).

The database is faked / overridden so nothing here needs Postgres.
"""

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import User


class FakeGetSession:
    """Minimal stand-in for AsyncSession.get (used by get_current_user)."""

    def __init__(self, user: Any) -> None:
        self._user = user

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._user


# --- get_current_user -------------------------------------------------------


async def test_get_current_user_valid_token() -> None:
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(user_id, organization_id)

    current = await get_current_user(token, FakeGetSession(user=object()))

    assert current == CurrentUser(user_id=user_id, organization_id=organization_id)


async def test_get_current_user_invalid_token() -> None:
    with pytest.raises(HTTPException) as exc:
        await get_current_user("not-a-token", FakeGetSession(user=object()))
    assert exc.value.status_code == 401


async def test_get_current_user_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_expires_minutes", -1)
    token = create_access_token(uuid.uuid4(), uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await get_current_user(token, FakeGetSession(user=object()))
    assert exc.value.status_code == 401


async def test_get_current_user_user_no_longer_exists() -> None:
    token = create_access_token(uuid.uuid4(), uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await get_current_user(token, FakeGetSession(user=None))
    assert exc.value.status_code == 401


# --- POST /auth/login -------------------------------------------------------


class FakeExecuteResult:
    def __init__(self, user: User | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> User | None:
        return self._user


class FakeLoginSession:
    """Stand-in for AsyncSession.execute(select(User)...) used by login."""

    def __init__(self, user: User | None) -> None:
        self._user = user

    async def execute(self, _stmt: Any) -> FakeExecuteResult:
        return FakeExecuteResult(self._user)


def _make_user(email: str, password: str) -> User:
    return User(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(password),
    )


async def _post_login(session: Any, username: str, password: str) -> Any:
    app.dependency_overrides[get_db] = lambda: session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/auth/login",
                data={"username": username, "password": password},
            )
    finally:
        app.dependency_overrides.clear()


async def test_login_success_returns_bearer_token() -> None:
    user = _make_user("alice@acme.test", "password123")

    response = await _post_login(FakeLoginSession(user), "alice@acme.test", "password123")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_unknown_email_and_wrong_password_are_identical_401() -> None:
    unknown = await _post_login(FakeLoginSession(None), "ghost@acme.test", "password123")

    user = _make_user("alice@acme.test", "password123")
    wrong = await _post_login(FakeLoginSession(user), "alice@acme.test", "not-the-password")

    assert unknown.status_code == wrong.status_code == 401
    # No user enumeration: the two responses are indistinguishable.
    assert unknown.json() == wrong.json()


# --- GET /auth/me (protected) -----------------------------------------------


async def test_me_requires_authentication() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_returns_identity_from_token() -> None:
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(user_id, organization_id)

    app.dependency_overrides[get_db] = lambda: FakeGetSession(user=object())
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user_id),
        "organization_id": str(organization_id),
    }
