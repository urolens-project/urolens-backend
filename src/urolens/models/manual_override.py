# Path: urolens-backend/src/urolens/models/manual_override.py
import uuid
from datetime import datetime
from sqlalchemy import ForeignKey, DateTime, String, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ManualOverride(Base):
    __tablename__ = "manual_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    parameter: Mapped[str] = mapped_column(String(100), nullable=False)

    # Both values are stored — original is NEVER overwritten (SRP: this table is the record)
    original_ai_value: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    corrected_value: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)

    overridden_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    overridden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Relationships
    result = relationship("AnalysisResult", back_populates="manual_overrides")
    overridden_by_user = relationship("User", foreign_keys=[overridden_by])

    def __repr__(self) -> str:
        return (
            f"<ManualOverride id={self.id} parameter={self.parameter!r} "
            f"original={self.original_ai_value} corrected={self.corrected_value}>"
        )