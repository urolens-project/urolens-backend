"""ORM model for the `consents` table."""
from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from .base import Base


class Consent(Base):
    """A patient's recorded consent answers, created alongside patient intake."""

    __tablename__ = "consents"

    consentId = Column("consent_id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    patientId = Column("patient_id", UUID(as_uuid=True), ForeignKey("patients.patient_id"), nullable=False)
    consentProcess = Column("consent_process", Boolean, nullable=False)
    consentStorage = Column("consent_storage", Boolean, nullable=False)
    consentResearch = Column("consent_research", Boolean, nullable=False)
    recordedAt = Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    recordedBy = Column("recorded_by", UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
