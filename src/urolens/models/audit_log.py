"""ORM model for the `audit_logs` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class AuditLog(Base):
    """Immutable, append-only record of every significant system event.
    Protected in production by a trigger that rejects UPDATE and DELETE.
    Source: Migration 0003 — SRS SO 4.4 RA 10173 audit trail requirement.
    """

    __tablename__ = "audit_logs"

    logId: Mapped[uuid.UUID] = mapped_column("log_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    eventType: Mapped[str] = mapped_column("event_type", String(80), nullable=False)
    entityType: Mapped[str] = mapped_column("entity_type", String(60), nullable=False)
    entityId: Mapped[uuid.UUID] = mapped_column("entity_id", UUID(as_uuid=True), nullable=False)
    userId: Mapped[uuid.UUID | None] = mapped_column("user_id", 
        UUID(as_uuid=True), nullable=True
    )
    ipAddress: Mapped[str | None] = mapped_column("ip_address", Text, nullable=True)
    detailJson: Mapped[dict[str, Any] | None] = mapped_column("detail_json", JSONB, nullable=True)
    occurredAt: Mapped[datetime] = mapped_column("occurred_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `log_id`, for callers expecting a generic `id` field."""
        return self.logId
