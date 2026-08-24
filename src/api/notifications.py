"""In-app notification routes and Expo push-token registration."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import getDb
from ..core.enums import UserRole
from ..core.rbac import RequireRole, getCurrentUser
from ..models.notification import Notification
from ..models.user import User
from ..schemas.notifications import NotificationOut, PushTokenRequest

_REQUIRE_MEDTECH = RequireRole([UserRole.MEDTECH])
router = APIRouter(prefix="/api/v1", tags=["notifications"])


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/notifications", response_model=list[NotificationOut])
async def listNotifications(
    currentUser: dict = Depends(_REQUIRE_MEDTECH),
    db: AsyncSession = Depends(getDb),
):
    """List the authenticated MedTech's 50 most recent notifications, newest first."""
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
    currentUser: dict = Depends(_REQUIRE_MEDTECH),
    db: AsyncSession = Depends(getDb),
):
    """Mark one of the authenticated MedTech's own notifications read.

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
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
