"""ORM model for the `specimen_rejections` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base

# specimen_rejections.reason_code is a native Postgres enum (type
# rejection_reason), not VARCHAR — same fix as Specimen.status/Image.status:
# a plain String mapping 500s on write. create_type=False since the type
# already exists in the DB.
_REJECTION_REASON = PgEnum(
    "INSUFFICIENT_VOLUME", "WRONG_CONTAINER", "UNLABELED", "OTHER",
    name="rejection_reason",
    create_type=False,
)


class SpecimenRejection(Base):
    """Rejection record logged when a specimen fails the receiving-desk visual
    check. Distinct from `Specimen.rejection_reason`/`rejected_at`, which
    record a *post-assignment* MedTech rejection of an already-received
    specimen — the two are separate workflows in the source system.
    Added via migration 0032 — table pre-existed the merge (created
    out-of-band, no prior Alembic history) and was accessed only via raw
    Supabase REST calls; this model formalizes its already-live schema.
    """

    __tablename__ = "specimen_rejections"

    rejectionId: Mapped[uuid.UUID] = mapped_column("rejection_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimenId: Mapped[uuid.UUID] = mapped_column("specimen_id", 
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    medtechId: Mapped[uuid.UUID] = mapped_column("medtech_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reasonCode: Mapped[str] = mapped_column("reason_code", _REJECTION_REASON, nullable=False)
    freeTextNote: Mapped[str | None] = mapped_column("free_text_note", Text, nullable=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `rejection_id`, for callers expecting a generic `id` field."""
        return self.rejectionId
