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
    """
    MedTech result confirmation event. One per result. Triggers Smart Diagnosis.
    Source: Migration 0015 — T2.5 Result Confirmation.
    """

    __tablename__ = "result_confirmations"

    confirmation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    medtech_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysis_result: Mapped["AnalysisResult"] = relationship(
        back_populates="confirmation"
    )

    @property
    def id(self) -> uuid.UUID:
        return self.confirmation_id

    @property
    def confirmed_by(self) -> uuid.UUID:
        return self.medtech_id
