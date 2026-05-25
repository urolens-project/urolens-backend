# Path: urolens-backend/src/urolens/models/result_confirmation.py
import uuid
from datetime import datetime
from sqlalchemy import ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ResultConfirmation(Base):
    __tablename__ = "result_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,  # one confirmation per result
        index=True,
    )
    confirmed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Relationships
    result = relationship("AnalysisResult", back_populates="confirmation")
    confirmed_by_user = relationship("User", foreign_keys=[confirmed_by])

    def __repr__(self) -> str:
        return f"<ResultConfirmation id={self.id} result_id={self.result_id}>"