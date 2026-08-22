"""Process-wide Supabase client, used by the Supabase-REST-backed
services/routers that haven't been ported to SQLAlchemy."""
from supabase import AsyncClient

from .config import settings

supabase = AsyncClient(settings.supabase_url, settings.supabase_service_key)
"""Module-level singleton Supabase client. Import and use directly, or via
`get_supabase` as a FastAPI dependency."""


async def get_supabase() -> AsyncClient:
    """FastAPI dependency returning the shared Supabase client.

    Returns:
        The module-level `supabase` client instance.
    """
    return supabase
