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

    escalation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    escalated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    escalation_path: Mapped[str] = mapped_column(String(30), nullable=False)
    escalation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        return self.escalation_id
