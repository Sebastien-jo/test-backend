"""Shared async Redis client.

A single connection-pool-backed client is created for the process and reused
across requests. It is closed on application shutdown (see `app.main`).
"""

from redis.asyncio import Redis

from app.core.config import settings

redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
