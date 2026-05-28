from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from src.urolens.models.base import Base

class Patient:
    """Utility schema definition mapping directly to the Supabase columns"""
    __tablename__ = "patients"

    # Map database 'patient_id' to an automatic tracking key (managed by Supabase uuid default)
    patient_id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    
    # This is where your custom format tracker 'URLNS-2026-XXXXX' will live!
    patient_uid = Column(String(30), unique=True, nullable=False, index=True)
    
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    
    # Supabase table expects text string representation for date of birth
    date_of_birth = Column(String, nullable=False) 
    
    # Column names must match your constraints (MALE, FEMALE, OTHER)
    sex = Column(String(10), nullable=False)
    
    contact_no = Column(String, nullable=True)
    address = Column(String, nullable=True)
    
    # Fields from your SQL layout matrix
    clinical_history = Column(String, nullable=True)
    is_walkin = Column(Boolean, nullable=False, default=False)
    record_flag = Column(String(20), nullable=False, default="COMPLETE")
    
    # CRITICAL: Table requires a valid user account ID tracking indicator string link
    registered_by = Column(UUID(as_uuid=True), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

class LabRequest:
    """Utility schema mapping variables directly to public.lab_requests columns"""
    __tablename__ = "lab_requests"