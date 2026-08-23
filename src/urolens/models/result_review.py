"""ORM model for the `result_reviews` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class ResultReview(Base):
    """Supervisor annotation on an analysis result, prior to approve/return/
    escalate. Source: migration 0019 — T3.2 Result Review.

    `spatial_annotations` was originally left unmapped (schema-drift finding
    from the row-7 port: no Alembic history anywhere in this repo for this
    column), which silently dropped every caller-supplied value —
    `save_annotation` accepted the field but never persisted it. Fixed via
    migration 0034: mapped as JSONB, matching the `Optional[List[Dict[str,
    Any]]]` shape confirmed from the pre-port
    `app/schemas/results.py`/`app/services/result_review_service.py` (git
    history, commit 35ab942^) and this codebase's existing convention for
    equivalently-shaped fields (`AnalysisResult.ai_findings`,
    `.flagged_anomalies`, `.particle_classes`, `SmartDiagnosisOutput.evidence_map`
    are all JSONB). The column type itself is still an inference from that
    pre-port code, not a live-database confirmation — see migration 0034's
    docstring.
    """

    __tablename__ = "result_reviews"

    reviewId: Mapped[uuid.UUID] = mapped_column("review_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reviewedBy: Mapped[uuid.UUID] = mapped_column("reviewed_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    annotationNotes: Mapped[str | None] = mapped_column("annotation_notes", Text, nullable=True)
    spatialAnnotations: Mapped[list[dict[str, Any]] | None] = mapped_column("spatial_annotations", 
        JSONB, nullable=True
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
        """Alias for `review_id`, for callers expecting a generic `id` field."""
        return self.reviewId
