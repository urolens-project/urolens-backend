from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Column, String, Boolean, Date, DateTime
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from pydantic import BaseModel
from datetime import date
import random

from database import engine, Base, get_db

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

Base.metadata.create_all(bind=engine)

app = FastAPI(title="UroLens LIS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Update the Pydantic schema to include complete_address
class PatientRegistrationRequest(BaseModel):
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: date
    gender: str
    contact_number: str
    complete_address: str  # 💡 Added this so frontend payload validates!
    is_walkin: bool
    emergency_name: str | None = None
    emergency_relationship: str | None = None
    emergency_phone: str | None = None

# --------------------------------------------------------------------------
# PATIENT INTAKE ROUTER ENDPOINT
# --------------------------------------------------------------------------
@app.post("/api/v1/intake/patients", status_code=status.HTTP_201_CREATED)
def register_patient_endpoint(payload: PatientRegistrationRequest, db: Session = Depends(get_db)):
    try:
        generated_sequence_id = f"URLNS-2026-{random.randint(10000, 99999)}"
        
        # Convert payload to a dictionary to use safe .get() lookups
        payload_data = payload.dict()
        is_walkin_session = payload_data.get("is_walkin", False)
        
        new_patient_record = Patient(
            id=generated_sequence_id,
            first_name=payload_data.get("first_name"),
            middle_name=payload_data.get("middle_name"),
            last_name=payload_data.get("last_name"),
            date_of_birth=payload_data.get("date_of_birth"),
            gender=payload_data.get("gender"),
            contact_number=payload_data.get("contact_number"),
            complete_address=payload_data.get("complete_address"),
            is_walkin=is_walkin_session,
            # Safely fall back to None if fields are missing from the frontend
            emergency_name=payload_data.get("emergency_name") if is_walkin_session else None,
            emergency_relationship=payload_data.get("emergency_relationship") if is_walkin_session else None,
            emergency_phone=payload_data.get("emergency_phone") if is_walkin_session else None,
        )
        
        db.add(new_patient_record)
        db.commit()
        db.refresh(new_patient_record)
        
        from datetime import datetime
        return {
            "success": True,
            "patient_id": generated_sequence_id,
            "message": "Patient serialization committed into core table matrix successfully.",
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database pipeline transaction write failure: {str(e)}"
        )