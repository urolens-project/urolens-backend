from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.schemas.sync import SyncPullResponse
from app.services import sync_service
from src.urolens.core.rbac import get_current_user

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.get("/pull", response_model=SyncPullResponse)
async def pull_sync(
    last_synced_at: Optional[datetime] = Query(
        None,
        description="ISO 8601 timestamp. If provided, returns only records updated after this time.",
    ),
    claims: dict = Depends(get_current_user),
):
    user_id = claims["user_id"]
    return await sync_service.pull(user_id, last_synced_at)
