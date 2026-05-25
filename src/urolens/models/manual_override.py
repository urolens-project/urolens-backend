from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class ManualOverride(Base):
    """
    MedTech correction to an individual AI-detected parameter.
    BOTH the original AI value AND the corrected value are stored.
    Source: Migration 0016 — T2.6 Manual Override.
    """

    __tablename__ = "manual_overrides"

    override_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    medtech_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parameter_name: Mapped[str] = mapped_column(String(60), nullable=False)
    original_ai_value: Mapped[int] = mapped_column(Integer, nullable=False)
    corrected_value: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysis_result: Mapped["AnalysisResult"] = relationship(
        back_populates="manual_overrides"
    )

    @property
    def id(self) -> uuid.UUID:
        return self.override_id
