"""ORM model for the `lab_requests` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class LabRequest(Base):
    """Physician-originated request for a lab test on a patient, prior to specimen
    receipt. Added via migration 0032 — table pre-existed the merge (created
    out-of-band, no prior Alembic history) and was accessed only via raw
    Supabase REST calls; this model formalizes its already-live schema.
    """

    __tablename__ = "lab_requests"

    labRequestId: Mapped[uuid.UUID] = mapped_column("lab_request_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    requestUid: Mapped[str] = mapped_column("request_uid", String(30), nullable=False, unique=True)
    patientId: Mapped[uuid.UUID] = mapped_column("patient_id", 
        UUID(as_uuid=True),
        ForeignKey("patients.patient_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    physicianId: Mapped[uuid.UUID | None] = mapped_column("physician_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    physicianName: Mapped[str | None] = mapped_column("physician_name", String(255), nullable=True)
    testType: Mapped[str] = mapped_column("test_type", String(50), nullable=False)
    clinicalNotes: Mapped[str | None] = mapped_column("clinical_notes", Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING_SAMPLE"
    )
    encodedBy: Mapped[uuid.UUID] = mapped_column("encoded_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column("updated_at", 
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `lab_request_id`, for callers expecting a generic `id` field."""
        return self.labRequestId
