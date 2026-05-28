from sqlalchemy import Boolean, Column, ForeignKey, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from .base import Base


class Consent(Base):
    __tablename__ = "consents"

    consent_id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.patient_id"), nullable=False)
    consent_process = Column(Boolean, nullable=False)
    consent_storage = Column(Boolean, nullable=False)
    consent_research = Column(Boolean, nullable=False)
    recorded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    recorded_by = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
