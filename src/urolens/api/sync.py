"""Mobile-client sync route."""
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.urolens.core.rbac import get_current_user
from src.urolens.schemas.sync import SyncPullResponse
from src.urolens.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.get("/pull", response_model=SyncPullResponse)
async def pull_sync(
    last_synced_at: datetime | None = Query(
        None,
        description="ISO 8601 timestamp. If provided, returns only records updated after this time.",
    ),
    claims: dict = Depends(get_current_user),
):
    """Pull a full or delta sync payload for the authenticated MedTech; see
    `sync_service.pull`.
    """
    user_id = claims["user_id"]
    return await sync_service.pull(user_id, last_synced_at)
