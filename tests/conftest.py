"""Shared test configuration.

Set throwaway settings so the application package can be imported without a real
environment.
These run at collection time, before any test module imports `app`, so the
module-level `Settings()` in `app.core.config` resolves. `setdefault` leaves a
real developer `.env` / exported vars untouched.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# >= 32 bytes so PyJWT does not warn about a weak HMAC key (RFC 7518).
os.environ.setdefault("JWT_SECRET", "test-secret-please-change-in-real-envs")
os.environ.setdefault("PARTNER_HMAC_SECRET", "test-partner-hmac-secret-change-me")
