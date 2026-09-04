"""ORM model for the `result_retrievals` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class ResultRetrieval(Base):
    """A physician's retrieval of one result's detail, logged by
    `PhysicianResultService.get_result_detail`.
    Source: Migration 0030 — physician result-detail retrieval logging.
    """

    __tablename__ = "result_retrievals"

    retrievalId: Mapped[uuid.UUID] = mapped_column("retrieval_id",
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id",
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="CASCADE"),
        nullable=False,
    )
    physicianId: Mapped[uuid.UUID | None] = mapped_column("physician_id",
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    retrievedAt: Mapped[datetime] = mapped_column("retrieved_at",
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ipAddress: Mapped[str | None] = mapped_column("ip_address", Text, nullable=True)

    analysisResult: Mapped[AnalysisResult] = relationship()

    @property
    def id(self) -> uuid.UUID:
        """Alias for `retrieval_id`, for callers expecting a generic `id` field."""
        return self.retrievalId
