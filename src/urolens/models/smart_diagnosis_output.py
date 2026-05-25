# Path: urolens-backend/src/urolens/models/smart_diagnosis_output.py
import uuid
import enum
from datetime import datetime
from sqlalchemy import ForeignKey, DateTime, Boolean, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ScoreLevel(str, enum.Enum):
    LOW      = "LOW"
    MODERATE = "MODERATE"
    HIGH     = "HIGH"


class SmartDiagnosisOutput(Base):
    __tablename__ = "smart_diagnosis_outputs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Three condition probability scores
    gout_score: Mapped[ScoreLevel] = mapped_column(
        SAEnum(ScoreLevel, name="score_level_enum"), nullable=False
    )
    gn_score: Mapped[ScoreLevel] = mapped_column(
        SAEnum(ScoreLevel, name="score_level_enum"), nullable=False
    )
    nephro_score: Mapped[ScoreLevel] = mapped_column(
        SAEnum(ScoreLevel, name="score_level_enum"), nullable=False
    )

    no_significant_indicators: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Evidence detail from the rule engine — stored as JSONB for flexibility
    evidence_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Relationships
    result = relationship("AnalysisResult", back_populates="smart_diagnosis_output")

    def __repr__(self) -> str:
        return (
            f"<SmartDiagnosisOutput id={self.id} "
            f"gout={self.gout_score} gn={self.gn_score} nephro={self.nephro_score}>"
        )