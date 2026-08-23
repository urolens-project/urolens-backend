"""ORM model for the `escalations` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class Escalation(Base):
    """Supervisor escalation record for an analysis result. Source: migration
    0019 — T3.2 Result Review.

    `escalation_path` is a native Postgres enum type on the live table
    (`CREATE TYPE escalation_path AS ENUM (...)`, migration 0019) but modeled
    here as a plain string, matching this codebase's established convention
    for status/enum-like columns elsewhere (Specimen.status,
    AnalysisResult.status, Specimen.rejection_reason — none use a SQLAlchemy
    Enum type). Valid values are validated in the service layer, same as the
    pre-port Supabase-REST code did.
    """

    __tablename__ = "escalations"

    escalationId: Mapped[uuid.UUID] = mapped_column("escalation_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    escalatedBy: Mapped[uuid.UUID] = mapped_column("escalated_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    escalationPath: Mapped[str] = mapped_column("escalation_path", String(30), nullable=False)
    escalationNote: Mapped[str | None] = mapped_column("escalation_note", Text, nullable=True)
    escalatedAt: Mapped[datetime] = mapped_column("escalated_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `escalation_id`, for callers expecting a generic `id` field."""
        return self.escalationId
