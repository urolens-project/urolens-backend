"""ORM model for the `notifications` table."""
from sqlalchemy import TIMESTAMP, VARCHAR, Boolean, Column, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .base import Base


class Notification(Base):
    """An in-app notification row, created by `NotificationService.notify`."""

    __tablename__ = "notifications"

    notification_id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(VARCHAR(100), nullable=False)
    entity_id = Column(UUID(as_uuid=True))
    is_read = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
