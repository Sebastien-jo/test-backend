"""Health endpoint.

Actively probes each backing dependency (Postgres, Redis) so the check
reflects real connectivity rather than just process liveness. Returns 503
when any dependency is unreachable, which lets orchestrators and the compose
healthcheck gate traffic on it.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import redis_client

router = APIRouter(tags=["health"])

DependencyStatus = Literal["up", "down"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    dependencies: dict[str, DependencyStatus]


async def _check_postgres(db: AsyncSession) -> bool:
    try:
        await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        return bool(await redis_client.ping())
    except Exception:
        return False


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HealthResponse:
    postgres_ok = await _check_postgres(db)
    redis_ok = await _check_redis()
    healthy = postgres_ok and redis_ok

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if healthy else "degraded",
        dependencies={
            "postgres": "up" if postgres_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
    )
