from supabase import AsyncClient

from app.config import SUPABASE_SERVICE_KEY, SUPABASE_URL

supabase = AsyncClient(SUPABASE_URL, SUPABASE_SERVICE_KEY)


async def get_supabase() -> AsyncClient:
    return supabase
