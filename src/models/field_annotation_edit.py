"""ORM model for the `field_annotation_edits` table (MedTech box edits)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import REAL, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .result_field import ResultField


class EditAction(enum.StrEnum):
    """What an edit did to a field's boxes."""

    REMOVED = "REMOVED"
    ADDED = "ADDED"


class FieldAnnotationEdit(Base):
    """One added or removed box on a field. Saving a field's edits supersedes the
    current rows (`supersededAt`) and inserts the new set; rows are never deleted.
    Source: SDD Module 2 multi-field proposal, section 6.2.
    """

    __tablename__ = "field_annotation_edits"
    __table_args__ = (
        CheckConstraint("action IN ('REMOVED', 'ADDED')", name="ck_field_annotation_edits_action"),
        CheckConstraint(
            "(action = 'REMOVED' AND detection_id IS NOT NULL) OR "
            "(action = 'ADDED' AND x IS NOT NULL AND y IS NOT NULL "
            "AND w IS NOT NULL AND h IS NOT NULL)",
            name="ck_field_annotation_edits_shape",
        ),
        Index(
            "ix_field_annotation_edits_current",
            "field_id",
            postgresql_where="superseded_at IS NULL",
        ),
    )

    editId: Mapped[uuid.UUID] = mapped_column(
        "edit_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fieldId: Mapped[uuid.UUID] = mapped_column(
        "field_id",
        UUID(as_uuid=True),
        ForeignKey("result_fields.field_id", name="fk_field_annotation_edits_field", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    detectionId: Mapped[str | None] = mapped_column("detection_id", String(64), nullable=True)
    """For REMOVED: the id inside `result_fields.detections`."""
    particleType: Mapped[str] = mapped_column("particle_type", String(60), nullable=False)
    x: Mapped[float | None] = mapped_column(REAL, nullable=True)
    y: Mapped[float | None] = mapped_column(REAL, nullable=True)
    w: Mapped[float | None] = mapped_column(REAL, nullable=True)
    h: Mapped[float | None] = mapped_column(REAL, nullable=True)
    """x, y, w, h are percent of the upright image (ADDED only)."""
    editedBy: Mapped[uuid.UUID] = mapped_column(
        "edited_by",
        UUID(as_uuid=True),
        ForeignKey("users.user_id", name="fk_field_annotation_edits_user", ondelete="RESTRICT"),
        nullable=False,
    )
    editedAt: Mapped[datetime] = mapped_column(
        "edited_at", DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    supersededAt: Mapped[datetime | None] = mapped_column(
        "superseded_at", DateTime(timezone=True), nullable=True
    )

    field: Mapped[ResultField] = relationship(back_populates="edits")
