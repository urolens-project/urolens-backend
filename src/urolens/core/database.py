"""SQLAlchemy async engine/session setup for the app's Postgres database, plus
the `get_db` FastAPI dependency routes use to get a request-scoped session."""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

# Re-export Supabase client used by legacy web-dev domain routers.
from .supabase import supabase  # noqa: F401

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
"""Process-wide async engine, bound to `settings.async_database_url`."""

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)
"""Session factory for `engine`. Prefer the `get_db` dependency over calling
this directly outside of a request context."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding one `AsyncSession` per request.

    On any exception raised while the session is in use, rolls back before
    re-raising; the session is always closed afterward regardless of outcome.

    Returns:
        An async generator yielding a single `AsyncSession`.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
