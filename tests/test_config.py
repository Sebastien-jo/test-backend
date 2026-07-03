"""Unit tests for application settings."""

import pytest

from app.core.config import Settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://host:6379/1")
    monkeypatch.setenv("JWT_SECRET", "jwt")
    monkeypatch.setenv("PARTNER_HMAC_SECRET", "hmac")

    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"
    assert settings.redis_url == "redis://host:6379/1"
    assert settings.jwt_secret == "jwt"
    assert settings.partner_hmac_secret == "hmac"
