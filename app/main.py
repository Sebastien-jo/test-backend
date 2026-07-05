"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.dev import router as dev_router
from app.api.documents import router as documents_router
from app.api.events import router as events_router
from app.api.health import router as health_router
from app.api.webhooks import router as webhooks_router
from app.core.config import settings
from app.core.db import engine
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """Release shared connections (DB engine, Redis client) on shutdown."""
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="Primmo Document Pipeline API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(events_router)
app.include_router(webhooks_router)

# Dev-only signing helper
if settings.dev_endpoints_enabled:
    app.include_router(dev_router)
