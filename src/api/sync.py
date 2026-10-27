"""Mobile-client sync route."""
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.sync import SyncPullResponse
from src.services import sync_service

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])

# Sync is the MedTech app's data feed; no other role has a device queue.
_medtech = RequireRole([UserRole.MEDTECH])


@router.get("/pull", response_model=SyncPullResponse)
async def pullSync(
    request: Request,
    lastSyncedAt: datetime | None = Query(
        None,
        description="ISO 8601 timestamp. If provided, returns only records updated after this time.",
    ),
    claims: dict = Depends(_medtech),
    db: AsyncSession = Depends(getDb),
):
    """Pull a full or delta sync payload for the authenticated MedTech; see
    `sync_service.pull`.
    """
    userId = claims["user_id"]
    return await sync_service.pull(db, userId, lastSyncedAt, request)
