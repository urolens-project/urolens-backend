from supabase import AsyncClient

from .config import settings

supabase = AsyncClient(settings.supabase_url, settings.supabase_service_key)


async def get_supabase() -> AsyncClient:
    return supabase
