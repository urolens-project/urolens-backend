"""ORM model for the `engine_error_logs` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult


class EngineErrorLog(Base):
    """AI rule engine runtime failures. Does not block MedTech confirmation.
    Source: Migration 0018 — T3.1 Alt Flow 2 Engine failure handling.

    error_code: INVALID_CLASSIFICATION | RULE_EVALUATION_FAILED | CONFIG_ERROR
    """

    __tablename__ = "engine_error_logs"

    errorId: Mapped[uuid.UUID] = mapped_column("error_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    errorCode: Mapped[str] = mapped_column("error_code", String(80), nullable=False)
    errorMessage: Mapped[str] = mapped_column("error_message", Text, nullable=False)
    stackTrace: Mapped[str | None] = mapped_column("stack_trace", Text, nullable=True)
    flaggedAt: Mapped[datetime] = mapped_column("flagged_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    analysisResult: Mapped[AnalysisResult] = relationship(
        back_populates="engineErrorLogs"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `error_id`, for callers expecting a generic `id` field."""
        return self.errorId
