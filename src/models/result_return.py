"""ORM model for the `result_returns` table."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base


class ResultReturn(Base):
    """Supervisor return-for-correction record for an analysis result.
    Source: migration 0019 — T3.2 Result Review.
    """

    __tablename__ = "result_returns"

    returnId: Mapped[uuid.UUID] = mapped_column("return_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    returnedBy: Mapped[uuid.UUID] = mapped_column("returned_by", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    returnedAt: Mapped[datetime] = mapped_column("returned_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `return_id`, for callers expecting a generic `id` field."""
        return self.returnId
