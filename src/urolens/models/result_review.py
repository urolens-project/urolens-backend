from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class ResultReview(Base):
    """
    Supervisor annotation on an analysis result, prior to approve/return/
    escalate. Source: migration 0019 — T3.2 Result Review.

    `spatial_annotations` (a JSON-ish column on the live table per
    app/services/result_review_service.py's usage) is deliberately not
    modeled here — it has no Alembic history anywhere in this repo (grepped
    every migration file; only this table's other four columns are covered,
    by 0019). Per this consolidation's schema-drift rule, no migration was
    added to cover it. `save_annotation` in the ported service persists
    `annotation_notes` only; a caller-supplied `spatial_annotations` value is
    accepted (for request-shape compatibility) but not persisted, and this is
    reported as a known gap rather than guessed at.
    """

    __tablename__ = "result_reviews"

    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    annotation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        return self.review_id
