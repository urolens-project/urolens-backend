from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class SmartDiagnosisOutput(Base):
    """
    Rule engine output per specimen. Generated automatically on result confirmation.
    Source: Migration 0017 — T3.1 Smart Diagnosis Engine.

    gout_level / glomerulonephritis_level / nephrolithiasis_level:
      One of 'LOW', 'MODERATE', 'HIGH'
    evidence_map: JSONB with per-condition evidence attribution
    """

    __tablename__ = "smart_diagnosis_outputs"

    output_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    # ── Diagnosis levels ─────────────────────────────────────────────────────
    gout_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    glomerulonephritis_level: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )
    nephrolithiasis_level: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )

    # ── Evidence attribution ─────────────────────────────────────────────────
    evidence_map: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysis_result: Mapped["AnalysisResult"] = relationship(
        back_populates="smart_diagnosis_output"
    )

    @property
    def id(self) -> uuid.UUID:
        return self.output_id
