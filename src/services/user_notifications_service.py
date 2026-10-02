"""What a signed-in user does with their own notifications (UROLENS-248).

List them (a preview, the full page with paging, or unread only), count the
unread ones for the bell badge, mark one or all read, and register or forget
the device that receives their pushes. Every query is scoped to the caller's
own `userId`, taken from their token, so any role can use these and no one can
see or change another user's notifications. Creating notifications and
sending pushes is `notification_service`'s job.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import NotFoundException
from ..models.notification import Notification
from ..models.user import User
from ..schemas.notifications import NotificationOut, NotificationUnreadCountResponse

logger = logging.getLogger(__name__)


def _notFound() -> NotFoundException:
    return NotFoundException(code="NOTIFICATION_NOT_FOUND", message="Notification not found.")


async def listNotifications(
    db: AsyncSession,
    userId: uuid.UUID,
    limit: int,
    before: uuid.UUID | None = None,
    unreadOnly: bool = False,
) -> list[NotificationOut]:
    """The caller's notifications, newest first.

    Args:
        db: the request's session.
        userId: the caller, from their token.
        limit: how many to return — a few for the bell's preview, a page for
            the full list.
        before: the last notification of the previous page; returns the ones
            older than it. Omitted, the list starts from the newest.
        unreadOnly: only notifications not yet read.

    Raises:
        NotFoundException: `NOTIFICATION_NOT_FOUND`, if `before` isn't one of
            the caller's notifications.
    """
    stmt = select(Notification).where(Notification.userId == userId)
    if unreadOnly:
        stmt = stmt.where(Notification.isRead.is_(False))
    if before is not None:
        cursor = (
            await db.execute(
                select(Notification.createdAt).where(
                    Notification.notificationId == before, Notification.userId == userId
                )
            )
        ).scalar_one_or_none()
        if cursor is None:
            raise _notFound()
        # The ID breaks ties between notifications created in the same instant,
        # so paging never skips or repeats one.
        stmt = stmt.where(
            tuple_(Notification.createdAt, Notification.notificationId) < tuple_(cursor, before)
        )
    stmt = stmt.order_by(Notification.createdAt.desc(), Notification.notificationId.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [NotificationOut.model_validate(row) for row in rows]


async def countUnreadNotifications(db: AsyncSession, userId: uuid.UUID) -> NotificationUnreadCountResponse:
    """How many of the caller's notifications are unread — all of them, not just one page."""
    count = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.userId == userId, Notification.isRead.is_(False))
        )
    ).scalar_one()
    return NotificationUnreadCountResponse(unreadCount=count)


async def markNotificationRead(db: AsyncSession, userId: uuid.UUID, notificationId: uuid.UUID) -> None:
    """Mark one of the caller's notifications read. Marking a read one again is fine.

    Raises:
        NotFoundException: `NOTIFICATION_NOT_FOUND`, if it doesn't exist or
            isn't the caller's.
    """
    result = await db.execute(
        update(Notification)
        .where(Notification.notificationId == notificationId, Notification.userId == userId)
        .values(is_read=True)
    )
    if result.rowcount == 0:
        raise _notFound()
    await db.commit()


async def markAllNotificationsRead(db: AsyncSession, userId: uuid.UUID) -> None:
    """Mark every unread notification of the caller's read."""
    await db.execute(
        update(Notification)
        .where(Notification.userId == userId, Notification.isRead.is_(False))
        .values(is_read=True)
    )
    await db.commit()


async def registerPushToken(db: AsyncSession, userId: uuid.UUID, token: str) -> None:
    """Make this device receive the caller's pushes, and only theirs.

    A device has one token. Removing it from anyone else who had it first means
    a shared phone stops receiving the previous user's notifications once the
    next user signs in (UROLENS-248).
    """
    await db.execute(
        update(User)
        .where(User.expoPushToken == token, User.userId != userId)
        .values(expo_push_token=None)
    )
    await db.execute(update(User).where(User.userId == userId).values(expo_push_token=token))
    await db.commit()


async def forgetPushToken(db: AsyncSession, userId: uuid.UUID) -> None:
    """Stop sending the caller's pushes to their device. Called on a manual logout.

    Never raises: the session is already closed, and a failure here mustn't
    turn a logout into an error. It is logged.
    """
    try:
        await db.execute(update(User).where(User.userId == userId).values(expo_push_token=None))
        await db.commit()
    except Exception:
        logger.exception("Failed to clear the push token of user %s at logout", userId)
