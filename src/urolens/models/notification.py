"""ORM model for the `notifications` table."""
from sqlalchemy import TIMESTAMP, VARCHAR, Boolean, Column, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .base import Base


class Notification(Base):
    """An in-app notification row, created by `NotificationService.notify`."""

    __tablename__ = "notifications"

    notificationId = Column("notification_id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    userId = Column("user_id", UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    message = Column(Text, nullable=False)
    notificationType = Column("notification_type", VARCHAR(100), nullable=False)
    entityId = Column("entity_id", UUID(as_uuid=True))
    isRead = Column("is_read", Boolean, nullable=False, server_default="false")
    createdAt = Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
