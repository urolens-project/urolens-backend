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
    from .specimen import Specimen
    from .image import Image
    from .result_confirmation import ResultConfirmation
    from .manual_override import ManualOverride
    from .smart_diagnosis_output import SmartDiagnosisOutput


class ResultStatus(str, enum.Enum):
    PENDING_CONFIRM = "PENDING_CONFIRM"
    PENDING_SUPERVISOR_APPROVAL = "PENDING_SUPERVISOR_APPROVAL"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"
    RETURNED_FOR_CORRECTION = "RETURNED_FOR_CORRECTION"
    CRITICAL_ESCALATED = "CRITICAL_ESCALATED"
    FAILED = "FAILED"


class AnalysisResult(Base):
    """
    AI inference output attached to a specimen. The central record of the
    MedTech and Supervisor workflows. One row per specimen.
    Source: Migration 0014 — T2.5, T2.6, T3.1, T3.2, T3.3, T3.4, T3.5.

    ai_findings: raw YOLOv8 output { "uric_acid_crystals": 12, "rbc_casts": 0, ... }
    flagged_anomalies: particles where AI count exceeds clinical threshold
    particle_classes: MedTech-confirmed classification after any overrides applied
    """

    __tablename__ = "analysis_results"

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    image_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("images.image_id", ondelete="RESTRICT"),
        nullable=True,
    )

    # ── AI Output ────────────────────────────────────────────────────────────
    ai_findings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    flagged_anomalies: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    particle_classes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    model_version: Mapped[str] = mapped_column(
        String(30), nullable=False, default="mvp-v1.0"
    )
    smart_diagnosis_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(48), nullable=False, default=ResultStatus.PENDING_CONFIRM
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────────
    specimen: Mapped["Specimen"] = relationship(back_populates="analysis_result")
    image: Mapped["Image | None"] = relationship(back_populates="analysis_result")
    confirmation: Mapped["ResultConfirmation | None"] = relationship(
        back_populates="analysis_result", uselist=False
    )
    manual_overrides: Mapped[list["ManualOverride"]] = relationship(
        back_populates="analysis_result"
    )
    smart_diagnosis_output: Mapped["SmartDiagnosisOutput | None"] = relationship(
        back_populates="analysis_result", uselist=False
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def id(self) -> uuid.UUID:
        return self.result_id

    @property
    def has_pending_retake(self) -> bool:
        return self.image is not None and self.image.is_discarded
