"""ORM model for result_confirmations.

TASK-MOB-09-2 | STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/app/models/result_confirmation.py

Single Responsibility: Data mapping for the result_confirmations table only.
Source: Migration 0015 — T2.5 Result Confirmation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from ../../../..app.db.base_class import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .user import User


class ResultConfirmation(Base):
    """Represents a MedTech's formal confirmation of an AI analysis result.

    One result may have at most one confirmation (enforced at DB and service level).
    Triggers Smart Diagnosis on creation.
    """

    __tablename__ = "result_confirmations"
    __table_args__ = (
        UniqueConstraint("result_id", name="uq_result_confirmations_result_id"),
    )

    # ── Primary key ────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Foreign keys ───────────────────────────────────────────────────────
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Payload ────────────────────────────────────────────────────────────
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING_SUPERVISOR_APPROVAL",
    )
    smart_diagnosis_triggered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Offline-first: False when created offline and not yet synced to server
    is_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Audit timestamps ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────
    result: Mapped[AnalysisResult] = relationship(
        "AnalysisResult",
        back_populates="confirmation",
        lazy="selectin",
    )
    confirmer: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[confirmed_by],
        lazy="selectin",
    )

    # ── Helpers / Backward Compatibility Aliases ───────────────────────────
    @property
    def is_pending_supervisor(self) -> bool:
        return self.status == "PENDING_SUPERVISOR_APPROVAL"

    @property
    def confirmation_id(self) -> uuid.UUID:
        """Alias for migration 0015 compatibility."""
        return self.id

    @property
    def medtech_id(self) -> uuid.UUID | None:
        """Alias for migration 0015 compatibility."""
        return self.confirmed_by

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ResultConfirmation id={self.id} result_id={self.result_id} "
            f"status={self.status}>"
        )