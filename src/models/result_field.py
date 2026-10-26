"""ORM model for the `result_fields` table (multi-field capture)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .field_annotation_edit import FieldAnnotationEdit
    from .image import Image
    from .manual_override import ManualOverride


class FieldStatus(enum.StrEnum):
    """Lifecycle of one field. Only ACCEPTED fields count toward the minimum and
    the averages."""

    ANALYZED = "ANALYZED"
    ACCEPTED = "ACCEPTED"
    DISCARDED = "DISCARDED"


class Magnification(enum.StrEnum):
    """Objective used for a field. Recorded now, not yet used in the averages."""

    HPF = "HPF"
    LPF = "LPF"


class ResultField(Base):
    """One captured microscope field of a specimen's result: its image, the AI
    output for it, the MedTech's corrections and the verification state.
    Source: SDD Module 2 multi-field proposal, section 6.1.

    `detections` is deferred: a field can hold up to 1,000 boxes, so it is loaded
    only when asked for (`undefer(ResultField.detections)`). Accessing it on an
    instance that did not load it raises in an async session, on purpose.
    """

    __tablename__ = "result_fields"
    __table_args__ = (
        UniqueConstraint("result_id", "field_no", name="uq_result_fields_result_field_no"),
        UniqueConstraint("image_id", name="uq_result_fields_image"),
        CheckConstraint("magnification IN ('HPF', 'LPF')", name="ck_result_fields_magnification"),
        CheckConstraint(
            "status IN ('ANALYZED', 'ACCEPTED', 'DISCARDED')", name="ck_result_fields_status"
        ),
        CheckConstraint("version >= 1", name="ck_result_fields_version"),
        Index("ix_result_fields_specimen_updated", "specimen_id", "updated_at"),
    )

    fieldId: Mapped[uuid.UUID] = mapped_column(
        "field_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column(
        "result_id",
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", name="fk_result_fields_result", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    specimenId: Mapped[uuid.UUID] = mapped_column(
        "specimen_id",
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", name="fk_result_fields_specimen", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    imageId: Mapped[uuid.UUID] = mapped_column(
        "image_id",
        UUID(as_uuid=True),
        ForeignKey("images.image_id", name="fk_result_fields_image", ondelete="RESTRICT"),
        nullable=False,
    )
    fieldNo: Mapped[int] = mapped_column("field_no", SmallInteger, nullable=False)
    """Capture order within the result. Never reused after a discard."""
    magnification: Mapped[str] = mapped_column(
        String(3), nullable=False, default=Magnification.HPF, server_default=Magnification.HPF.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FieldStatus.ANALYZED,
        server_default=FieldStatus.ANALYZED.value,
    )

    # ── AI output ────────────────────────────────────────────────────────────
    aiFindings: Mapped[dict[str, Any]] = mapped_column(
        "ai_findings", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    detections: Mapped[list[dict[str, Any]]] = mapped_column(
        "detections", JSONB, nullable=False, default=list,
        server_default=text("'[]'::jsonb"), deferred=True,
    )
    """`[{id, particleType, confidence, x, y, w, h}]`, boxes in percent of the
    upright image, ids assigned by the backend when stored."""
    imageWidth: Mapped[int] = mapped_column("image_width", Integer, nullable=False)
    imageHeight: Mapped[int] = mapped_column("image_height", Integer, nullable=False)
    confidenceScores: Mapped[dict[str, Any]] = mapped_column(
        "confidence_scores", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    effectiveCounts: Mapped[dict[str, Any]] = mapped_column(
        "effective_counts", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    smartDiagnosis: Mapped[dict[str, Any] | None] = mapped_column(
        "smart_diagnosis", JSONB, nullable=True
    )
    smartDiagnosisUnavailable: Mapped[bool] = mapped_column(
        "smart_diagnosis_unavailable", Boolean, nullable=False, default=False,
        server_default=text("false"),
    )
    modelVersion: Mapped[str] = mapped_column("model_version", String(30), nullable=False)
    discardReason: Mapped[str | None] = mapped_column("discard_reason", Text, nullable=True)

    # ── Verification ─────────────────────────────────────────────────────────
    verifiedBy: Mapped[uuid.UUID | None] = mapped_column(
        "verified_by",
        UUID(as_uuid=True),
        ForeignKey("users.user_id", name="fk_result_fields_verified_by", ondelete="RESTRICT"),
        nullable=True,
    )
    verifiedAt: Mapped[datetime | None] = mapped_column(
        "verified_at", DateTime(timezone=True), nullable=True
    )
    lastOpenedBy: Mapped[uuid.UUID | None] = mapped_column(
        "last_opened_by",
        UUID(as_uuid=True),
        ForeignKey("users.user_id", name="fk_result_fields_opened_by", ondelete="RESTRICT"),
        nullable=True,
    )
    lastOpenedAt: Mapped[datetime | None] = mapped_column(
        "last_opened_at", DateTime(timezone=True), nullable=True
    )
    lastCorrectedAt: Mapped[datetime | None] = mapped_column(
        "last_corrected_at", DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    """Optimistic-concurrency token for annotation saves."""

    # ── Timestamps ───────────────────────────────────────────────────────────
    createdAt: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysisResult: Mapped[AnalysisResult] = relationship(back_populates="fields")
    image: Mapped[Image] = relationship(back_populates="field")
    edits: Mapped[list[FieldAnnotationEdit]] = relationship(
        back_populates="field", order_by="FieldAnnotationEdit.editedAt", lazy="raise"
    )
    manualOverrides: Mapped[list[ManualOverride]] = relationship(
        back_populates="field", lazy="raise"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `field_id`, for callers expecting a generic `id` field."""
        return self.fieldId
