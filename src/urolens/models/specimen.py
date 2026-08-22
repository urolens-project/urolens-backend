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

    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lab_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    sample_uid: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="RECEIVED"
    )
    visual_check_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    received_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Denormalized intake/labeling fields ─────────────────────────────────
    # Added via migration 0032 — these columns already existed on the live table
    # (written/read by the pre-merge Supabase-REST code) but were never modeled.
    medtech_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    patient_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Fernet-encrypted ciphertext (see core.encryption.encrypt_pii) — never plaintext."""
    patient_uid: Mapped[str | None] = mapped_column(String(30), nullable=True)
    test_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    priority_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ROUTINE"
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rejection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────────
    images: Mapped[list[Image]] = relationship(
        back_populates="specimen", cascade="save-update, merge"
    )
    analysis_result: Mapped[AnalysisResult | None] = relationship(
        back_populates="specimen", uselist=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `specimen_id`, for callers expecting a generic `id` field."""
        return self.specimen_id
