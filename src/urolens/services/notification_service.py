import logging
import uuid

import httpx
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.enums import UserRole
from ..models.notification import Notification
from ..models.user import User

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


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
            return

        # Best-effort push delivery — never raises
        await self._push(user_id, message, notification_type, entity_id)

    async def notify_supervisor_result_ready(
        self,
        result_id: uuid.UUID,
        specimen_id: uuid.UUID,
    ) -> None:
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
        supervisor_ids = await self._get_supervisor_ids()
        for sup_id in supervisor_ids:
            await self.notify(
                user_id=sup_id,
                message="Smart Diagnosis is unavailable for a confirmed result due to an engine error.",
                notification_type="SMART_DIAGNOSIS_UNAVAILABLE",
                entity_id=result_id,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _push(
        self,
        user_id: uuid.UUID,
        message: str,
        notification_type: str,
        entity_id: uuid.UUID | None,
    ) -> None:
        token = await self._get_push_token(user_id)
        if not token or not token.startswith("ExponentPushToken"):
            return
        payload = {
            "to": token,
            "title": "UroLens",
            "body": message,
            "data": {
                "notification_type": notification_type,
                "entity_id": str(entity_id) if entity_id else None,
            },
            "sound": "default",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(EXPO_PUSH_URL, json=payload)
        except Exception:
            logger.warning("Expo push delivery failed for user %s", user_id)

    async def _get_push_token(self, user_id: uuid.UUID) -> str | None:
        try:
            stmt = select(User.expo_push_token).where(User.user_id == user_id)
            result = await self.db.execute(stmt)
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def _get_supervisor_ids(self) -> list[uuid.UUID]:
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
