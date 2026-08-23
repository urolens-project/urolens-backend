"""ORM model for the `analysis_results` table."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .engine_error_log import EngineErrorLog
    from .image import Image
    from .manual_override import ManualOverride
    from .patient import Patient
    from .result_confirmation import ResultConfirmation
    from .result_view import ResultView
    from .smart_diagnosis_output import SmartDiagnosisOutput
    from .specimen import Specimen
    from .user import User


class ResultStatus(str, enum.Enum):
    """Lifecycle states for an `AnalysisResult`, spanning the confirm ->
    override -> approve -> release chain plus retake/escalation/failure exits.
    """

    PENDING_CONFIRM = "PENDING_CONFIRM"
    PENDING_SUPERVISOR_APPROVAL = "PENDING_SUPERVISOR_APPROVAL"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"
    RETURNED_FOR_CORRECTION = "RETURNED_FOR_CORRECTION"
    CRITICAL_ESCALATED = "CRITICAL_ESCALATED"
    IMAGE_RETAKE_REQUESTED = "IMAGE_RETAKE_REQUESTED"
    FAILED = "FAILED"


class AnalysisResult(Base):
    """AI inference output attached to a specimen. The central record of the
    MedTech and Supervisor workflows. One row per specimen.
    Source: Migration 0014 — T2.5, T2.6, T3.1, T3.2, T3.3, T3.4, T3.5.

    ai_findings: raw YOLOv8 output { "uric_acid_crystals": 12, "rbc_casts": 0, ... }
    flagged_anomalies: particles where AI count exceeds clinical threshold
    particle_classes: MedTech-confirmed classification after any overrides applied
    """

    __tablename__ = "analysis_results"

    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimenId: Mapped[uuid.UUID] = mapped_column("specimen_id", 
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    imageId: Mapped[uuid.UUID | None] = mapped_column("image_id", 
        UUID(as_uuid=True),
        ForeignKey("images.image_id", ondelete="RESTRICT"),
        nullable=True,
    )

    # ── AI Output ────────────────────────────────────────────────────────────
    aiFindings: Mapped[dict[str, Any]] = mapped_column("ai_findings", 
        JSONB, nullable=False, default=dict
    )
    flaggedAnomalies: Mapped[dict[str, Any]] = mapped_column("flagged_anomalies", 
        JSONB, nullable=False, default=dict
    )
    particleClasses: Mapped[dict[str, Any]] = mapped_column("particle_classes", 
        JSONB, nullable=False, default=dict
    )
    modelVersion: Mapped[str] = mapped_column("model_version", 
        String(30), nullable=False, default="mvp-v1.0"
    )
    smartDiagnosis: Mapped[dict[str, Any] | None] = mapped_column("smart_diagnosis", 
        JSONB, nullable=True
    )
    smartDiagnosisUnavailable: Mapped[bool] = mapped_column("smart_diagnosis_unavailable", 
        Boolean, nullable=False, default=False
    )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(48), nullable=False, default=ResultStatus.PENDING_CONFIRM
    )
    confirmedBy: Mapped[uuid.UUID | None] = mapped_column("confirmed_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmedAt: Mapped[datetime | None] = mapped_column("confirmed_at", 
        DateTime(timezone=True), nullable=True
    )

    # ── Patient Portal ───────────────────────────────────────────────────────
    patientId: Mapped[uuid.UUID | None] = mapped_column("patient_id", 
        UUID(as_uuid=True),
        ForeignKey("patients.patient_id", ondelete="RESTRICT"),
        nullable=True,
    )
    cellCounts: Mapped[dict | None] = mapped_column("cell_counts", 
        JSONB, nullable=True
    )
    interpretation: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    medtechId: Mapped[uuid.UUID | None] = mapped_column("medtech_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    medtechName: Mapped[str | None] = mapped_column("medtech_name", 
        String(255), nullable=True
    )
    pathologistName: Mapped[str | None] = mapped_column("pathologist_name", 
        String(255), nullable=True
    )
    pathologistLicense: Mapped[str | None] = mapped_column("pathologist_license", 
        String(100), nullable=True
    )
    releasedAt: Mapped[datetime | None] = mapped_column("released_at", 
        DateTime(timezone=True), nullable=True
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column("updated_at", 
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────────
    specimen: Mapped[Specimen] = relationship(back_populates="analysisResult")
    image: Mapped[Image | None] = relationship(back_populates="analysisResult")
    confirmation: Mapped[ResultConfirmation | None] = relationship(
        back_populates="analysisResult", uselist=False
    )
    manualOverrides: Mapped[list[ManualOverride]] = relationship(
        back_populates="analysisResult"
    )
    smartDiagnosisOutput: Mapped[SmartDiagnosisOutput | None] = relationship(
        back_populates="analysisResult", uselist=False
    )
    engineErrorLogs: Mapped[list[EngineErrorLog]] = relationship(
        back_populates="analysisResult"
    )
    resultViews: Mapped[list[ResultView]] = relationship(
        back_populates="analysisResult"
    )
    patient: Mapped[Patient | None] = relationship(
        back_populates="analysisResults"
    )
    medtech: Mapped[User | None] = relationship(
        foreign_keys=[medtechId]
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def id(self) -> uuid.UUID:
        """Alias for `result_id`, for callers expecting a generic `id` field."""
        return self.resultId

    @property
    def hasPendingRetake(self) -> bool:
        """Whether this result's current image has been discarded and needs a retake."""
        return self.image is not None and self.image.isDiscarded
