"""In-app notification routes and Expo push-token registration; see
`services/user_notifications_service.py`.
"""
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import getDb
from ..core.rbac import getCurrentUser
from ..schemas.notifications import (
    NotificationOut,
    NotificationUnreadCountResponse,
    PushTokenRequest,
)
from ..services import user_notifications_service

router = APIRouter(prefix="/api/v1", tags=["notifications"])

# The default page keeps `GET /notifications` returning what it always has.
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


# ── Endpoints ─────────────────────────────────────────────────────────────────
# Every route here is scoped to the caller's own user_id in the query itself
# (never trusted from the request), so any authenticated role can call
# these — there's nothing MedTech-specific about "list my notifications".
# Previously hardcoded to MEDTECH only, which meant every other role's
# notifications (RESULT_READY_FOR_REVIEW to Supervisors, RESULT_RELEASED to
# Patients/Physicians, LAB_REQUEST_SUBMITTED to Receptionists) were created
# but unreadable by anyone but a mobile push landing on a registered device.

@router.get("/notifications", response_model=list[NotificationOut])
async def listNotifications(
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    before: uuid.UUID | None = Query(default=None),
    unreadOnly: bool = Query(default=False),
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> list[NotificationOut]:
    """List the caller's notifications, newest first; see `user_notifications_service.listNotifications`."""
    return await user_notifications_service.listNotifications(
        db, uuid.UUID(currentUser["user_id"]), limit=limit, before=before, unreadOnly=unreadOnly
    )


@router.get("/notifications/unread-count", response_model=NotificationUnreadCountResponse)
async def countUnreadNotifications(
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> NotificationUnreadCountResponse:
    """The bell badge; see `user_notifications_service.countUnreadNotifications`."""
    return await user_notifications_service.countUnreadNotifications(db, uuid.UUID(currentUser["user_id"]))


@router.patch("/notifications/read-all", status_code=204)
async def markAllNotificationsRead(
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """Mark all the caller's notifications read; see `user_notifications_service.markAllNotificationsRead`."""
    await user_notifications_service.markAllNotificationsRead(db, uuid.UUID(currentUser["user_id"]))


@router.patch("/notifications/{notification_id}/read", status_code=204)
async def markNotificationRead(
    notification_id: uuid.UUID,
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """Mark one of the caller's notifications read; see `user_notifications_service.markNotificationRead`."""
    await user_notifications_service.markNotificationRead(
        db, uuid.UUID(currentUser["user_id"]), notification_id
    )


@router.post("/users/push-token", status_code=204)
async def registerPushToken(
    body: PushTokenRequest,
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """Register the caller's device for pushes; see `user_notifications_service.registerPushToken`."""
    await user_notifications_service.registerPushToken(db, uuid.UUID(currentUser["user_id"]), body.token)
