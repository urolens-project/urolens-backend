from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from datetime import date, datetime
import random

# Import the cloud API client instance directly
from src.urolens.core.database import supabase 

router = APIRouter(
    prefix="/api/v1/intake",
    tags=["Patient Intake"]
)

REAL_USER_ID = "2c1c8ecb-b751-42ce-b372-a82e304b1a65"

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

@router.get("/patients/search")
def search_patients_endpoint(q: str):
    try:
        # Search for records matching the text pattern against first or last names
        response = supabase.table("patients")\
            .select("patient_id, patient_uid, first_name, last_name")\
            .or_(f"first_name.ilike.%{q}%,last_name.ilike.%{q}%,patient_uid.ilike.%{q}%")\
            .limit(5)\
            .execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/patients", status_code=status.HTTP_201_CREATED)
def register_patient_endpoint(payload: PatientRegistrationRequest):
    try:
        generated_sequence_id = f"URLNS-2026-{random.randint(10000, 99999)}"
        payload_data = payload.dict()
        
        incoming_gender = payload_data.get("gender", "OTHER")
        if incoming_gender:
            incoming_gender = incoming_gender.upper()
        
        # Package the keys to match your Supabase column names exactly
        patient_record_payload = {
            "patient_uid": generated_sequence_id,
            "first_name": payload_data.get("first_name"),
            "middle_name": payload_data.get("middle_name"),
            "last_name": payload_data.get("last_name"),
            "date_of_birth": str(payload_data.get("date_of_birth")),
            "sex": incoming_gender,
            "contact_no": payload_data.get("contact_number"),
            "address": payload_data.get("complete_address"),
            "is_walkin": payload_data.get("is_walkin", False),
            "registered_by": REAL_USER_ID,
            "record_flag": "COMPLETE"
        }
        
        # Shoot the data packet straight up to the Supabase REST API layer
        response = supabase.table("patients").insert(patient_record_payload).execute()
        
        return {
            "success": True,
            "patient_id": generated_sequence_id,
            "message": "Patient serialization committed into cloud table matrix successfully.",
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Supabase Cloud API write failure: {str(e)}"
        )