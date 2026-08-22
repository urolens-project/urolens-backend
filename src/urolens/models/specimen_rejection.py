"""ORM model for the `specimen_rejections` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class SpecimenRejection(Base):
    """
    Rejection record logged when a specimen fails the receiving-desk visual
    check. Distinct from `Specimen.rejection_reason`/`rejected_at`, which
    record a *post-assignment* MedTech rejection of an already-received
    specimen — the two are separate workflows in the source system.
    Added via migration 0032 — table pre-existed the merge (created
    out-of-band, no prior Alembic history) and was accessed only via raw
    Supabase REST calls; this model formalizes its already-live schema.
    """

    __tablename__ = "specimen_rejections"

    rejection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    medtech_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    free_text_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `rejection_id`, for callers expecting a generic `id` field."""
        return self.rejection_id
