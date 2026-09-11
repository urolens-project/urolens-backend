"""ORM model for the `result_releases` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult

# result_releases.release_method is a native Postgres enum (type
# release_method), not VARCHAR — mapping it as String let SQLAlchemy send
# inserts as a bare VARCHAR bind param, which Postgres refuses to implicitly
# cast ("column is of type release_method but expression is of type
# character varying"), making every release attempt 500. Same fix as
# Specimen.status; create_type=False since the type already exists in the DB.
_RELEASE_METHOD = PgEnum("PHYSICAL", "DIGITAL", name="release_method", create_type=False)


class ResultRelease(Base):
    """Supervisor/receptionist release event for one `AnalysisResult`, created
    by `ResultReleasingService.release_result`.
    Source: Migration 0026 — STORY-WEB-15 Result Releasing (T4.1).
    """

    __tablename__ = "result_releases"

    releaseId: Mapped[uuid.UUID] = mapped_column("release_id",
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id",
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    releasedBy: Mapped[uuid.UUID] = mapped_column("released_by",
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    releaseMethod: Mapped[str] = mapped_column("release_method", _RELEASE_METHOD, nullable=False)
    releasedAt: Mapped[datetime] = mapped_column("released_at",
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    analysisResult: Mapped[AnalysisResult] = relationship()

    @property
    def id(self) -> uuid.UUID:
        """Alias for `release_id`, for callers expecting a generic `id` field."""
        return self.releaseId
