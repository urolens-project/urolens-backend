"""ORM model for the `print_jobs` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base

# print_jobs.status is a native Postgres enum (type print_job_status), not
# VARCHAR — same fix as Specimen.status/Image.status: a plain String mapping
# 500s on write. create_type=False since the type already exists in the DB.
_PRINT_JOB_STATUS = PgEnum("SENT", "PRINTED", "FAILED", name="print_job_status", create_type=False)


class PrintJob(Base):
    """Print-queue record for a specimen label. Added via migration 0032 — table
    pre-existed the merge (created out-of-band, no prior Alembic history) and
    was accessed only via raw Supabase REST calls; this model formalizes its
    already-live schema.
    """

    __tablename__ = "print_jobs"

    printJobId: Mapped[uuid.UUID] = mapped_column("print_job_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    labelId: Mapped[uuid.UUID] = mapped_column("label_id", 
        UUID(as_uuid=True),
        ForeignKey("sample_labels.label_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    specimenId: Mapped[uuid.UUID] = mapped_column("specimen_id", 
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(_PRINT_JOB_STATUS, nullable=False, default="SENT")
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `print_job_id`, for callers expecting a generic `id` field."""
        return self.printJobId
