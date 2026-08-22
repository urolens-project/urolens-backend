"""ORM model for the `print_jobs` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class PrintJob(Base):
    """
    Print-queue record for a specimen label. Added via migration 0032 — table
    pre-existed the merge (created out-of-band, no prior Alembic history) and
    was accessed only via raw Supabase REST calls; this model formalizes its
    already-live schema.
    """

    __tablename__ = "print_jobs"

    print_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sample_labels.label_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    specimen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SENT")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `print_job_id`, for callers expecting a generic `id` field."""
        return self.print_job_id
