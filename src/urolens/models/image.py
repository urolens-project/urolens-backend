"""ORM model for the `images` table."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .specimen import Specimen


class ImageStatus(enum.StrEnum):
    """Lifecycle states for an uploaded microscopy image; see `Image.status`."""

    ACTIVE = "ACTIVE"
    DISCARDED = "DISCARDED"
    REPLACED = "REPLACED"


class Image(Base):
    """Microscopy image metadata. Binary stored in Supabase Storage (see
    `services/ai_integration_service.py` — no AWS credentials exist anywhere
    in this project, so this was previously and incorrectly documented as S3;
    corrected here, along with an unimplemented "EXIF stripped before upload"
    claim that no code in this repo actually performs).
    Source: Migration 0013 — T2.7 Image Retake / Re-upload.

    status:
      ACTIVE    — image is attached to the specimen and in use
      DISCARDED — MedTech discarded it; retake required
      REPLACED  — superseded by a newer upload for the same specimen
    """

    __tablename__ = "images"

    image_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── S3 storage ────────────────────────────────────────────────────────────
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── Validated dimensions ─────────────────────────────────────────────────
    width_px: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    height_px: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ImageStatus.ACTIVE
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────────
    specimen: Mapped[Specimen] = relationship(back_populates="images")
    analysis_result: Mapped[AnalysisResult | None] = relationship(
        back_populates="image", uselist=False
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def id(self) -> uuid.UUID:
        """Alias for `image_id`, for callers expecting a generic `id` field."""
        return self.image_id

    @property
    def is_discarded(self) -> bool:
        """Whether this image has been discarded (retake flow)."""
        return self.status == ImageStatus.DISCARDED

    @property
    def meets_minimum_resolution(self) -> bool:
        """Whether this image meets the 640×480 minimum required for AI analysis."""
        return self.width_px >= 640 and self.height_px >= 480
