"""ORM model for the `users` table."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    pass


class User(Base):
    """All system users across all roles. Passwords are bcrypt-hashed.
    Source: Migration 0001 — T2.1 Login/Logout, cross-cutting RBAC.
    """

    __tablename__ = "users"

    userId: Mapped[uuid.UUID] = mapped_column("user_id", 
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    hashedPassword: Mapped[str] = mapped_column("hashed_password", Text, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    isActive: Mapped[bool] = mapped_column("is_active", Boolean, nullable=False, default=True)
    failedAttempts: Mapped[int] = mapped_column("failed_attempts", SmallInteger, nullable=False, default=0)
    lockedAt: Mapped[datetime | None] = mapped_column("locked_at", 
        DateTime(timezone=True), nullable=True
    )
    expoPushToken: Mapped[str | None] = mapped_column("expo_push_token", Text, nullable=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", 
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column("updated_at", 
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ── Helpers ──────────────────────────────────────────────────────────────
    @property
    def id(self) -> uuid.UUID:
        """Alias for `user_id`, for callers expecting a generic `id` field."""
        return self.userId

    @property
    def isLocked(self) -> bool:
        """Whether the account is currently locked out (`locked_at` is set)."""
        return self.lockedAt is not None

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role}>"
