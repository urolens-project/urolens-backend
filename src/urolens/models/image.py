# src/urolens/models/image.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base  # shared declarative base

if TYPE_CHECKING:
    from .specimen import Specimen
    from .user import User
    from .analysis_result import AnalysisResult


class ImageStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    DISCARDED = "DISCARDED"


class Image(Base):
    """
    Stores metadata for a microscopy image uploaded by a MedTech.

    The binary content lives in S3; this row holds validation data,
    lifecycle status, and the FK references needed for audit.
    """

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── S3 Storage ───────────────────────────────────────────────────────────
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    s3_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ── Validated dimensions ─────────────────────────────────────────────────
    # Backend re-validates even though the client enforces 640×480 minimum.
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Lifecycle ────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ImageStatus.UPLOADED
    )
    discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discarded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
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
    specimen: Mapped["Specimen"] = relationship(back_populates="images")
    uploader: Mapped["User"] = relationship(foreign_keys=[uploaded_by])
    analysis_result: Mapped["AnalysisResult | None"] = relationship(
        back_populates="image", uselist=False
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def is_discarded(self) -> bool:
        return self.status == ImageStatus.DISCARDED

    @property
    def meets_minimum_resolution(self) -> bool:
        return self.width_px >= 640 and self.height_px >= 480