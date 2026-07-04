"""Synchronous DB access for Celery workers."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# Reuse the same database but with the sync psycopg driver.
_sync_url = settings.database_url.replace("+asyncpg", "+psycopg")

engine = create_engine(
    _sync_url,
    pool_size=settings.celery_worker_concurrency,
    max_overflow=8,
    pool_pre_ping=True,
)

SessionFactory = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def worker_session() -> Iterator[Session]:
    """Yield a short-lived worker session, always closed at the end."""
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()
