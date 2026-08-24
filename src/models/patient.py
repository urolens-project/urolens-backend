"""ORM model for the `patients` table."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, VARCHAR, Boolean, Column, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    pass


class Patient(Base):
    """A patient record, created by `PatientService.create_patient`. PII
    fields (`first_name`, `last_name`, `date_of_birth`, `contact_no`,
    `address`) are Fernet-encrypted ciphertext, not plaintext.
    """

    __tablename__ = "patients"

    patientId = Column("patient_id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    patientUid = Column("patient_uid", VARCHAR(20), unique=True, nullable=False)
    firstName = Column("first_name", Text, nullable=False)
    middleName = Column("middle_name", Text, nullable=True)
    """Fernet-encrypted ciphertext (see core.encryption.encrypt_pii) — never plaintext."""
    lastName = Column("last_name", Text, nullable=False)
    dateOfBirth = Column("date_of_birth", Text, nullable=False)
    contactNo = Column("contact_no", Text)
    address = Column(Text)
    clinicalHistory = Column("clinical_history", Text, nullable=True)
    """Plaintext, unlike the PII fields above — matches PatientCreateRequest, which
    never encrypts this field either. Added via migration 0033; confirmed live and
    in active use by PatientService before this column was modeled (same
    out-of-band-schema pattern found and documented in migration 0032)."""
    isWalkin = Column("is_walkin", Boolean, nullable=False, server_default="false")
    recordFlag = Column("record_flag", VARCHAR(50))
    createdBy = Column("created_by", UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    userId = Column("user_id", UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, unique=True)
    createdAt = Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updatedAt = Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    analysisResults = relationship("AnalysisResult", back_populates="patient", foreign_keys="[AnalysisResult.patientId]")
    resultViews = relationship("ResultView", back_populates="patient")
