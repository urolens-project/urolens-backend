"""ORM model for the `queue_assignments` table."""
from sqlalchemy import Column, ForeignKey, VARCHAR, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from .base import Base


class QueueAssignment(Base):
    """A specimen's assignment to a MedTech, created by `QueueService.assign_specimen`."""

    __tablename__ = "queue_assignments"

    assignment_id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    specimen_id = Column(UUID(as_uuid=True), ForeignKey("specimens.specimen_id"), nullable=False)
    medtech_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    assigned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    status = Column(VARCHAR(50), nullable=False, server_default="ACTIVE")
