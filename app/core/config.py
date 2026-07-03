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


settings = Settings()
