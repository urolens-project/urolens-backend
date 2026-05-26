import logging
from uuid import UUID
from supabase import AsyncClient

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, db: AsyncClient):
        self.db = db

    async def notify(
        self,
        user_id: UUID,
        message: str,
        notification_type: str,
        entity_id: UUID | None = None,
    ) -> None:
        try:
            await self.db.table("notifications").insert({
                "user_id": str(user_id),
                "message": message,
                "notification_type": notification_type,
                "entity_id": str(entity_id) if entity_id else None,
            }).execute()
        except Exception:
            logger.exception("Failed to create notification for user %s", user_id)
