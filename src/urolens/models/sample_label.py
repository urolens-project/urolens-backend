"""ORM model for the `sample_labels` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class SampleLabel(Base):
    """Printed specimen label record. Added via migration 0032 — table pre-existed
    the merge (created out-of-band, no prior Alembic history) and was accessed
    only via raw Supabase REST calls; this model formalizes its already-live
    schema.
    """

    __tablename__ = "sample_labels"

    labelId: Mapped[uuid.UUID] = mapped_column("label_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    specimenId: Mapped[uuid.UUID] = mapped_column("specimen_id", 
        UUID(as_uuid=True),
        ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    sampleUid: Mapped[str | None] = mapped_column("sample_uid", String(30), nullable=True)
    labelContentJson: Mapped[dict[str, Any]] = mapped_column("label_content_json", 
        JSONB, nullable=False, default=dict
    )
    generatedBy: Mapped[uuid.UUID] = mapped_column("generated_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    affixedConfirmed: Mapped[bool] = mapped_column("affixed_confirmed", 
        Boolean, nullable=False, default=False
    )
    affixedAt: Mapped[datetime | None] = mapped_column("affixed_at", 
        DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `label_id`, for callers expecting a generic `id` field."""
        return self.labelId
