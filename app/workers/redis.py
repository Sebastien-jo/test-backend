"""Synchronous Redis client for the Celery workers."""

from redis import Redis

from app.core.config import settings

redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
