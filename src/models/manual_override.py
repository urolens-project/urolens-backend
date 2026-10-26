"""ORM model for the `manual_overrides` table."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .result_field import ResultField


class OverrideKind(enum.StrEnum):
    """For a field and class the latest row decides: OVERRIDE sets the count,
    REVERT returns it to the box count."""

    OVERRIDE = "OVERRIDE"
    REVERT = "REVERT"


class ManualOverride(Base):
    """MedTech correction to an individual AI-detected parameter.
    BOTH the original AI value AND the corrected value are stored.
    Source: Migration 0016 — T2.6 Manual Override.
    """

    __tablename__ = "manual_overrides"
    __table_args__ = (
        CheckConstraint("kind IN ('OVERRIDE', 'REVERT')", name="ck_manual_overrides_kind"),
    )

    overrideId: Mapped[uuid.UUID] = mapped_column("override_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resultId: Mapped[uuid.UUID] = mapped_column("result_id", 
        UUID(as_uuid=True),
        ForeignKey("analysis_results.result_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    medtechId: Mapped[uuid.UUID] = mapped_column("medtech_id", 
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parameterName: Mapped[str] = mapped_column("parameter_name", String(60), nullable=False)
    originalAiValue: Mapped[str] = mapped_column("original_ai_value", Text, nullable=False)
    correctedValue: Mapped[str] = mapped_column("corrected_value", Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    overriddenAt: Mapped[datetime] = mapped_column("overridden_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    fieldId: Mapped[uuid.UUID | None] = mapped_column(
        "field_id",
        UUID(as_uuid=True),
        ForeignKey("result_fields.field_id", name="fk_manual_overrides_field", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """Set on every override written by the multi-field workflow. NULL on older
    result-level rows, which stay as history."""
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default=OverrideKind.OVERRIDE,
        server_default=OverrideKind.OVERRIDE.value,
    )

    # ── Relationships ────────────────────────────────────────────────────────
    analysisResult: Mapped[AnalysisResult] = relationship(
        back_populates="manualOverrides"
    )
    field: Mapped[ResultField | None] = relationship(
        back_populates="manualOverrides", lazy="raise"
    )

    @property
    def id(self) -> uuid.UUID:
        """Alias for `override_id`, for callers expecting a generic `id` field."""
        return self.overrideId

    @property
    def parameter(self) -> str:
        """Alias for `parameter_name`, matching the API-contract field name."""
        return self.parameterName

    @property
    def overriddenBy(self) -> uuid.UUID:
        """Alias for `medtech_id`, matching the API-contract field name."""
        return self.medtechId
