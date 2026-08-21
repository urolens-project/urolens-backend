from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class LabRequest(Base):
    """
    Physician-originated request for a lab test on a patient, prior to specimen
    receipt. Added via migration 0032 — table pre-existed the merge (created
    out-of-band, no prior Alembic history) and was accessed only via raw
    Supabase REST calls; this model formalizes its already-live schema.
    """

    __tablename__ = "lab_requests"

    lab_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_uid: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patients.patient_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    physician_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    physician_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    test_type: Mapped[str] = mapped_column(String(50), nullable=False)
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING_SAMPLE"
    )
    encoded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def id(self) -> uuid.UUID:
        return self.lab_request_id
