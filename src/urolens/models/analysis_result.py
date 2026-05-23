# src/urolens/models/analysis_result.py
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
    PENDING_REVIEW = "PENDING_REVIEW"
    PENDING_SUPERVISOR_APPROVAL = "PENDING_SUPERVISOR_APPROVAL"
    APPROVED = "APPROVED"
    RETURNED_FOR_CORRECTION = "RETURNED_FOR_CORRECTION"
    FAILED = "FAILED"


class AnalysisResult(Base):
    """
    The central record for a specimen's AI analysis.

    ai_findings is the raw JSONB from urolens_ai.infer():
      { "RBC": 12, "WBC": 4, "Epithelial": 0, "Bacteria": 0, ... }

    smart_diagnosis_json is populated after smart_diagnosis_service.run().
    smart_diagnosis_unavailable is set true when the rule engine fails —
    the confirmation still succeeds but the Supervisor is notified.
    """

    __tablename__ = "analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    image_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("images.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── AI Output ────────────────────────────────────────────────────────────
    ai_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    smart_diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    smart_diagnosis_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(48), nullable=False, default=ResultStatus.PENDING_REVIEW
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
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
    image: Mapped["Image"] = relationship(back_populates="analysis_result")
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
    def has_pending_retake(self) -> bool:
        """True if the linked image was discarded — blocks confirmation."""
        return self.image is not None and self.image.is_discarded