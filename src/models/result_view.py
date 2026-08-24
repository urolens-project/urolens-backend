"""ORM model for the `result_views` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .patient import Patient


class ResultView(Base):
    """A patient's view of a released result, recorded by
    `PatientResultService.get_result_detail`.
    """

    __tablename__ = "result_views"

    viewId: Mapped[uuid.UUID] = mapped_column("view_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="CASCADE"),
        nullable=False,
    )
    patientId: Mapped[uuid.UUID] = mapped_column("patient_id", 
        UUID(as_uuid=True),
        ForeignKey("patients.patient_id", ondelete="CASCADE"),
        nullable=False,
    )
    viewedAt: Mapped[datetime] = mapped_column("viewed_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    analysisResult: Mapped[AnalysisResult] = relationship(
        back_populates="resultViews"
    )
    patient: Mapped[Patient] = relationship(
        back_populates="resultViews"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `view_id`, for callers expecting a generic `id` field."""
        return self.viewId
