"""ORM model for the `queue_assignments` table."""
from sqlalchemy import TIMESTAMP, VARCHAR, Column, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .base import Base


class QueueAssignment(Base):
    """A specimen's assignment to a MedTech, created by `QueueService.assign_specimen`."""

    __tablename__ = "queue_assignments"

    assignmentId = Column("assignment_id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    specimenId = Column("specimen_id", UUID(as_uuid=True), ForeignKey("specimens.specimen_id"), nullable=False)
    medtechId = Column("medtech_id", UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    assignedBy = Column("assigned_by", UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    assignedAt = Column("assigned_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    status = Column(VARCHAR(50), nullable=False, server_default="ACTIVE")
