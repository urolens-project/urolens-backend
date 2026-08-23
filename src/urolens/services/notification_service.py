"""In-app notification rows plus best-effort Expo push delivery."""
import logging
import uuid

import httpx
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import UserRole
from ..models.notification import Notification
from ..models.user import User

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


class NotificationService:
    """Creates `Notification` rows and attempts best-effort Expo push delivery."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def notify(
        self,
        userId: uuid.UUID,
        message: str,
        notificationType: str,
        entityId: uuid.UUID | None = None,
    ) -> None:
        """Insert a notification row and attempt push delivery. Never raises —
        a DB failure is logged and swallowed; push delivery failures are
        swallowed inside `_push`.

        Args:
            entity_id: the related entity (e.g. a result or specimen ID), if any.
        """
        try:
            stmt = insert(Notification).values(
                user_id=userId,
                message=message,
                notification_type=notificationType,
                entity_id=entityId,
            )
            await self.db.execute(stmt)
        except Exception:
            logger.exception("Failed to create notification for user %s", userId)
            return

        # Best-effort push delivery — never raises
        await self._push(userId, message, notificationType, entityId)

    async def notifySupervisorResultReady(
        self,
        resultId: uuid.UUID,
        specimenId: uuid.UUID,
    ) -> None:
        """Notify every active supervisor that a result is ready for their review."""
        supervisorIds = await self._getActiveUserIds(UserRole.SUPERVISOR)
        for supId in supervisorIds:
            await self.notify(
                userId=supId,
                message=f"A result is ready for your review (specimen {specimenId}).",
                notificationType="RESULT_READY_FOR_REVIEW",
                entityId=resultId,
            )

    async def notifySupervisorDiagnosisUnavailable(
        self,
        resultId: uuid.UUID,
    ) -> None:
        """Notify every active supervisor that Smart Diagnosis failed for a
        confirmed result.
        """
        supervisorIds = await self._getActiveUserIds(UserRole.SUPERVISOR)
        for supId in supervisorIds:
            await self.notify(
                userId=supId,
                message="Smart Diagnosis is unavailable for a confirmed result due to an engine error.",
                notificationType="SMART_DIAGNOSIS_UNAVAILABLE",
                entityId=resultId,
            )

    async def notifyActiveReceptionists(
        self,
        requestUid: str,
        physicianName: str,
        labRequestId: uuid.UUID,
    ) -> None:
        """Notify every active receptionist that a physician submitted a new
        lab request needing follow-up (e.g. specimen collection).
        """
        receptionistIds = await self._getActiveUserIds(UserRole.RECEPTIONIST)
        for recId in receptionistIds:
            await self.notify(
                userId=recId,
                message=f"New lab request {requestUid} submitted by Dr. {physicianName}.",
                notificationType="LAB_REQUEST_SUBMITTED",
                entityId=labRequestId,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _push(
        self,
        userId: uuid.UUID,
        message: str,
        notificationType: str,
        entityId: uuid.UUID | None,
    ) -> None:
        # Best-effort Expo push delivery; no-ops if the user has no token, and
        # swallows any HTTP failure — push is never allowed to break notify().
        token = await self._getPushToken(userId)
        if not token or not token.startswith("ExponentPushToken"):
            return
        payload = {
            "to": token,
            "title": "UroLens",
            "body": message,
            "data": {
                "notification_type": notificationType,
                "entity_id": str(entityId) if entityId else None,
            },
            "sound": "default",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(EXPO_PUSH_URL, json=payload)
        except Exception:
            logger.warning("Expo push delivery failed for user %s", userId)

    async def _getPushToken(self, userId: uuid.UUID) -> str | None:
        # Returns None (rather than raising) on lookup failure or missing token.
        try:
            stmt = select(User.expoPushToken).where(User.userId == userId)
            result = await self.db.execute(stmt)
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def _getActiveUserIds(self, role: UserRole) -> list[uuid.UUID]:
        # Returns [] (rather than raising) on query failure.
        try:
            stmt = select(User.userId).where(
                User.role == role,
                User.isActive.is_(True),
            )
            rows = await self.db.execute(stmt)
            return list(rows.scalars().all())
        except Exception:
            logger.exception("Failed to query active %s IDs for notification", role)
            return []
