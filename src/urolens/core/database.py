from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Re-export Base so legacy web-dev domain modules that import
# `from src.urolens.core.database import Base` continue to work.
from ..models.base import Base  # noqa: F401

from .config import DATABASE_URL

# Re-export Supabase client used by legacy web-dev domain routers.
try:
    from app.db.supabase import supabase  # noqa: F401
except Exception:
    supabase = None  # type: ignore[assignment]

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
