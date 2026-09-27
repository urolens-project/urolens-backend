"""In-app notification routes and Expo push-token registration."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import getDb
from ..core.exceptions import NotFoundException
from ..core.rbac import getCurrentUser
from ..models.notification import Notification
from ..models.user import User
from ..schemas.notifications import NotificationOut, PushTokenRequest

router = APIRouter(prefix="/api/v1", tags=["notifications"])


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
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
):
    """List the authenticated user's 50 most recent notifications, newest first."""
    userId = uuid.UUID(currentUser["user_id"])
    stmt = (
        select(Notification)
        .where(Notification.userId == userId)
        .order_by(Notification.createdAt.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.patch("/notifications/{notification_id}/read", status_code=204)
async def markNotificationRead(
    notification_id: uuid.UUID,
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
):
    """Mark one of the authenticated user's own notifications read.

    Raises:
        HTTPException: 404, if `notification_id` doesn't exist or doesn't
            belong to the caller.
    """
    userId = uuid.UUID(currentUser["user_id"])
    stmt = (
        update(Notification)
        .where(
            Notification.notificationId == notification_id,
            Notification.userId == userId,
        )
        .values(is_read=True)
    )
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise NotFoundException(message="Notification not found.")
    await db.commit()


@router.patch("/notifications/read-all", status_code=204)
async def markAllNotificationsRead(
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
):
    """Mark all of the authenticated user's unread notifications read."""
    userId = uuid.UUID(currentUser["user_id"])
    stmt = (
        update(Notification)
        .where(Notification.userId == userId, Notification.isRead.is_(False))
        .values(is_read=True)
    )
    await db.execute(stmt)
    await db.commit()


@router.post("/users/push-token", status_code=204)
async def registerPushToken(
    body: PushTokenRequest,
    currentUser: dict = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
):
    """Register or update the authenticated user's Expo push token for
    mobile push notifications.
    """
    userId = uuid.UUID(currentUser["user_id"])
    stmt = (
        update(User)
        .where(User.userId == userId)
        .values(expo_push_token=body.token)
    )
    await db.execute(stmt)
    await db.commit()
