"""ORM model for the `smart_diagnosis_outputs` table."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class ScoreLevel(enum.StrEnum):
    """The three risk levels a Smart Diagnosis condition score can take."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class SmartDiagnosisOutput(Base):
    """Rule engine output per specimen. Generated automatically on result confirmation.
    Source: Migration 0017 — T3.1 Smart Diagnosis Engine.

    gout_score / gn_score / nephro_score: LOW / MODERATE / HIGH
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

    # ── Diagnosis scores (Gout / Glomerulonephritis / Nephrolithiasis) ────────
    gout_score: Mapped[str] = mapped_column(String(10), nullable=False)
    gn_score: Mapped[str] = mapped_column(String(10), nullable=False)
    nephro_score: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── Aggregate flags ───────────────────────────────────────────────────────
    no_significant_indicators: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ── Evidence attribution ──────────────────────────────────────────────────
    evidence_map: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    # ── Engine metadata ───────────────────────────────────────────────────────
    engine_version: Mapped[str] = mapped_column(
        String(30), nullable=False, default="mvp-v1.0"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="ATTACHED"
    )

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    analysis_result: Mapped[AnalysisResult] = relationship(
        back_populates="smart_diagnosis_output"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `output_id`, for callers expecting a generic `id` field."""
        return self.output_id
