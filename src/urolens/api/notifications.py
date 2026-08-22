"""In-app notification routes and Expo push-token registration."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.enums import UserRole
from ..core.rbac import RequireRole, get_current_user
from ..models.notification import Notification
from ..models.user import User
from ..schemas.notifications import NotificationOut, PushTokenRequest

router = APIRouter(prefix="/api/v1", tags=["notifications"])


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    db: AsyncSession = Depends(get_db),
):
    """List the authenticated MedTech's 50 most recent notifications, newest first."""
    user_id = uuid.UUID(current_user["user_id"])
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.patch("/notifications/{notification_id}/read", status_code=204)
async def mark_notification_read(
    notification_id: uuid.UUID,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    db: AsyncSession = Depends(get_db),
):
    """Mark one of the authenticated MedTech's own notifications read.

    Raises:
        HTTPException: 404, if `notification_id` doesn't exist or doesn't
            belong to the caller.
    """
    user_id = uuid.UUID(current_user["user_id"])
    stmt = (
        update(Notification)
        .where(
            Notification.notification_id == notification_id,
            Notification.user_id == user_id,
        )
        .values(is_read=True)
    )
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    await db.commit()


@router.post("/users/push-token", status_code=204)
async def register_push_token(
    body: PushTokenRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register or update the authenticated user's Expo push token for
    mobile push notifications."""
    user_id = uuid.UUID(current_user["user_id"])
    stmt = (
        update(User)
        .where(User.user_id == user_id)
        .values(expo_push_token=body.token)
    )
    await db.execute(stmt)
    await db.commit()
