import logging
import uuid

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.notification import Notification
from ..models.user import User, UserRole

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def notify(
        self,
        user_id: uuid.UUID,
        message: str,
        notification_type: str,
        entity_id: uuid.UUID | None = None,
    ) -> None:
        try:
            stmt = insert(Notification).values(
                user_id=user_id,
                message=message,
                notification_type=notification_type,
                entity_id=entity_id,
            )
            await self.db.execute(stmt)
        except Exception:
            logger.exception("Failed to create notification for user %s", user_id)

    async def notify_supervisor_result_ready(
        self,
        result_id: uuid.UUID,
        specimen_id: uuid.UUID,
    ) -> None:
        """Notify all active supervisors that a result is ready for review."""
        supervisor_ids = await self._get_supervisor_ids()
        for sup_id in supervisor_ids:
            await self.notify(
                user_id=sup_id,
                message=f"A result is ready for your review (specimen {specimen_id}).",
                notification_type="RESULT_READY_FOR_REVIEW",
                entity_id=result_id,
            )

    async def notify_supervisor_diagnosis_unavailable(
        self,
        result_id: uuid.UUID,
    ) -> None:
        """Notify all active supervisors that smart diagnosis failed for a result."""
        supervisor_ids = await self._get_supervisor_ids()
        for sup_id in supervisor_ids:
            await self.notify(
                user_id=sup_id,
                message="Smart Diagnosis is unavailable for a confirmed result due to an engine error.",
                notification_type="SMART_DIAGNOSIS_UNAVAILABLE",
                entity_id=result_id,
            )

    async def _get_supervisor_ids(self) -> list[uuid.UUID]:
        """Returns all active SUPERVISOR user IDs."""
        try:
            stmt = select(User.user_id).where(
                User.role == UserRole.SUPERVISOR,
                User.is_active.is_(True),
            )
            rows = await self.db.execute(stmt)
            return list(rows.scalars().all())
        except Exception:
            logger.exception("Failed to query supervisor IDs for notification")
            return []
