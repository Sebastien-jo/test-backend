"""Async database engine and session management.

A single async engine is created for the process. `get_db` yields a session
scoped to a request and is meant to be used as a FastAPI dependency.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a database session, closing it when the request ends."""
    async with SessionFactory() as session:
        yield session
