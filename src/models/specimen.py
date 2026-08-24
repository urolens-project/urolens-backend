"""ORM model for the `specimens` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .image import Image


class Specimen(Base):
    """Physical urine specimen record.
    Source: Migration 0007 — T1.4 Sample Receiving.
    """

    __tablename__ = "specimens"

    specimenId: Mapped[uuid.UUID] = mapped_column("specimen_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    labRequestId: Mapped[uuid.UUID] = mapped_column("lab_request_id", 
        UUID(as_uuid=True), nullable=False, index=True
    )
    sampleUid: Mapped[str | None] = mapped_column("sample_uid", String(30), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="RECEIVED"
    )
    visualCheckPassed: Mapped[bool] = mapped_column("visual_check_passed", 
        Boolean, nullable=False, default=True
    )
    receivedBy: Mapped[uuid.UUID] = mapped_column("received_by", 
        UUID(as_uuid=True), nullable=False
    )
    receivedAt: Mapped[datetime] = mapped_column("received_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assignedAt: Mapped[datetime | None] = mapped_column("assigned_at", 
        DateTime(timezone=True), nullable=True
    )
    completedAt: Mapped[datetime | None] = mapped_column("completed_at", 
        DateTime(timezone=True), nullable=True
    )
    updatedAt: Mapped[datetime] = mapped_column("updated_at", 
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Denormalized intake/labeling fields ─────────────────────────────────
    # Added via migration 0032 — these columns already existed on the live table
    # (written/read by the pre-merge Supabase-REST code) but were never modeled.
    medtechId: Mapped[uuid.UUID | None] = mapped_column("medtech_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    patientName: Mapped[str | None] = mapped_column("patient_name", Text, nullable=True)
    """Fernet-encrypted ciphertext (see core.encryption.encrypt_pii) — never plaintext."""
    patientUid: Mapped[str | None] = mapped_column("patient_uid", String(30), nullable=True)
    testType: Mapped[str | None] = mapped_column("test_type", String(50), nullable=True)
    priorityLevel: Mapped[str] = mapped_column("priority_level", 
        String(20), nullable=False, default="ROUTINE"
    )
    rejectionReason: Mapped[str | None] = mapped_column("rejection_reason", String(30), nullable=True)
    rejectionNote: Mapped[str | None] = mapped_column("rejection_note", Text, nullable=True)
    rejectedAt: Mapped[datetime | None] = mapped_column("rejected_at", 
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────────
    images: Mapped[list[Image]] = relationship(
        back_populates="specimen", cascade="save-update, merge"
    )
    analysisResult: Mapped[AnalysisResult | None] = relationship(
        back_populates="specimen", uselist=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `specimen_id`, for callers expecting a generic `id` field."""
        return self.specimenId
