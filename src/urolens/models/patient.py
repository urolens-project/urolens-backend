from __future__ import annotations

from typing import TYPE_CHECKING
from sqlalchemy import Boolean, Column, ForeignKey, Text, VARCHAR, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base

if TYPE_CHECKING:
    from .analysis_result import AnalysisResult
    from .result_view import ResultView


class Patient(Base):
    __tablename__ = "patients"

    patient_id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    patient_uid = Column(VARCHAR(20), unique=True, nullable=False)
    first_name = Column(Text, nullable=False)
    last_name = Column(Text, nullable=False)
    date_of_birth = Column(Text, nullable=False)
    contact_no = Column(Text)
    address = Column(Text)
    is_walkin = Column(Boolean, nullable=False, server_default="false")
    record_flag = Column(VARCHAR(50))
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, unique=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    analysis_results = relationship("AnalysisResult", back_populates="patient", foreign_keys="[AnalysisResult.patient_id]")
    result_views = relationship("ResultView", back_populates="patient")
