from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .specimen import Specimen


class UserRole(str, enum.Enum):
    RECEPTIONIST = "RECEPTIONIST"
    MEDTECH = "MEDTECH"
    SUPERVISOR = "SUPERVISOR"
    PHYSICIAN = "PHYSICIAN"
    PATIENT = "PATIENT"
    ADMINISTRATOR = "ADMINISTRATOR"


class User(Base):
    """
    All system users across all roles. Passwords are bcrypt-hashed.
    Source: Migration 0001 — T2.1 Login/Logout, cross-cutting RBAC.
    """

    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expo_push_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def id(self) -> uuid.UUID:
        return self.user_id

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role}>"
