from sqlalchemy import Column, String, Boolean, Date, DateTime
from sqlalchemy.sql import func
from src.urolens.core.database import Base

class Patient(Base):
    __tablename__ = "patients"

    id = Column(String, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    middle_name = Column(String, nullable=True)
    last_name = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=False)
    gender = Column(String, nullable=False)
    contact_number = Column(String, nullable=False)
    complete_address = Column(String, nullable=False)
    is_walkin = Column(Boolean, default=False)
    emergency_name = Column(String, nullable=True)
    emergency_relationship = Column(String, nullable=True)
    emergency_phone = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())