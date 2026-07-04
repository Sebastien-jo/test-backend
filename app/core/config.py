"""Application configuration loaded from the environment.

Settings are read once at import time from environment variables (and, in
local development, from a `.env` file). Field names are matched to env vars
case-insensitively, so `DATABASE_URL` populates `database_url`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    database_url: str
    redis_url: str
    jwt_secret: str
    partner_hmac_secret: str

    # Access-token lifetime in minutes.
    jwt_expires_minutes: int = 60

    storage_backend: str = "local"
    storage_path: str = "/data/uploads"
    # Max accepted upload size, in bytes (default 20 MiB).
    max_upload_bytes: int = 20 * 1024 * 1024

    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_worker_concurrency: int = 32


settings = Settings()
