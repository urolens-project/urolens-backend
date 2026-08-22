"""ORM model for the `images` table.

Note: the `Image` class docstring below states binary storage is S3, but
`services/ai_integration_service.py` explicitly documents (and this repo's
actual upload path uses) Supabase Storage, not S3/boto3 — no AWS credentials
exist anywhere in this project. That's a pre-existing doc/reality mismatch,
not corrected here since this pass is documentation-only; see the
flagged-findings changelog entry."""
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
    from .specimen import Specimen
    from .analysis_result import AnalysisResult


class ImageStatus(str, enum.Enum):
    """Lifecycle states for an uploaded microscopy image; see `Image.status`."""

    ACTIVE = "ACTIVE"
    DISCARDED = "DISCARDED"
    REPLACED = "REPLACED"


class Image(Base):
    """
    Microscopy image metadata. Binary stored in S3. EXIF stripped before upload.
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
    specimen: Mapped["Specimen"] = relationship(back_populates="images")
    analysis_result: Mapped["AnalysisResult | None"] = relationship(
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
