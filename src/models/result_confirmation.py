"""ORM model for the `result_confirmations` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class ResultConfirmation(Base):
    """MedTech result confirmation event. One per result. Triggers Smart Diagnosis.
    Source: Migration 0015 — T2.5 Result Confirmation.
    """

    __tablename__ = "result_confirmations"

    confirmationId: Mapped[uuid.UUID] = mapped_column("confirmation_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    medtechId: Mapped[uuid.UUID] = mapped_column("medtech_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmedAt: Mapped[datetime] = mapped_column("confirmed_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysisResult: Mapped[AnalysisResult] = relationship(
        back_populates="confirmation"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `confirmation_id`, for callers expecting a generic `id` field."""
        return self.confirmationId

    @property
    def confirmedBy(self) -> uuid.UUID:
        """Alias for `medtech_id`, matching the API-contract field name."""
        return self.medtechId
