"""Mobile-client sync route."""
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.core.rbac import getCurrentUser
from src.schemas.sync import SyncPullResponse
from src.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.get("/pull", response_model=SyncPullResponse)
async def pullSync(
    lastSyncedAt: datetime | None = Query(
        None,
        description="ISO 8601 timestamp. If provided, returns only records updated after this time.",
    ),
    claims: dict = Depends(getCurrentUser),
):
    """Pull a full or delta sync payload for the authenticated MedTech; see
    `sync_service.pull`.
    """
    userId = claims["user_id"]
    return await sync_service.pull(userId, lastSyncedAt)
