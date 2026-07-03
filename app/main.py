"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.health import router as health_router
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
