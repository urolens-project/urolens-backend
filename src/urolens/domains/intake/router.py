from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import date, datetime
import random

from src.urolens.core.database import get_db
from src.urolens.domains.intake.models import Patient

router = APIRouter(
    prefix="/api/v1/intake",
    tags=["Patient Intake"]
)

class PatientRegistrationRequest(BaseModel):
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: date
    gender: str
    contact_number: str
    complete_address: str
    is_walkin: bool
    emergency_name: str | None = None
    emergency_relationship: str | None = None
    emergency_phone: str | None = None

@router.post("/patients", status_code=status.HTTP_201_CREATED)
def register_patient_endpoint(payload: PatientRegistrationRequest, db: Session = Depends(get_db)):
    try:
        generated_sequence_id = f"URLNS-2026-{random.randint(10000, 99999)}"
        
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
            # Safely fall back to None if fields are missing from the frontend form
            emergency_name=payload_data.get("emergency_name") if is_walkin_session else None,
            emergency_relationship=payload_data.get("emergency_relationship") if is_walkin_session else None,
            emergency_phone=payload_data.get("emergency_phone") if is_walkin_session else None,
        )
        
        db.add(new_patient_record)
        db.commit()
        db.refresh(new_patient_record)
        
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