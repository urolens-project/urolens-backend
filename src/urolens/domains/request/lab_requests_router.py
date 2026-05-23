from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import random
import uuid

from src.urolens.core.database import supabase 

router = APIRouter(
    prefix="/api/v1/lab-requests",
    tags=["Lab Requests"]
)

CURRENT_ENCODER_ID = "2c1c8ecb-b751-42ce-b372-a82e304b1a65"

# 1. CORE SYSTEM PHYSICIAN KEY MAP
# Matches names to the user_id primary keys in your Supabase 'users' table
PHYSICIAN_UUID_MAP = {
    "Dr. Vince Serato": "2c1c8ecb-b751-42ce-b372-a82e304b1a65", # Replace with actual user_id from your DB if different
    "Dr. Maria Santos": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "Dr. Juan Dela Cruz": "f4e3d2c1-b0a9-8m7n-6p5q-4r3s2t1u0v9w"
}

class LabRequestCreatePayload(BaseModel):
    patient_id: uuid.UUID
    physician_id: Optional[uuid.UUID] = None # Change this from name to an optional UUID!
    physician_name: Optional[str] = None     # Used as a fallback if manual entry is toggled
    test_type: str
    clinical_notes: Optional[str] = None

@router.get("/physicians", status_code=status.HTTP_200_OK)
def get_physicians_endpoint():
    try:
        response = supabase.table("users")\
            .select("user_id, username")\
            .eq("role", "PHYSICIAN")\
            .eq("is_active", True)\
            .execute()

        if response.data is None:
            return []

        return response.data

    except Exception as e:
        logger.error(f"Physicians fetch failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query database physician registry: {str(e)}"
        )

@router.post("", status_code=status.HTTP_201_CREATED)
def create_lab_request_endpoint(payload: LabRequestCreatePayload):
    try:
        generated_request_uid = f"REQ-2026-{random.randint(10000, 99999)}"
        payload_data = payload.dict()
        
        computed_id = payload_data.get("physician_id")
        computed_name = payload_data.get("physician_name")
        
        # If they selected a database doctor, look up their name to keep lab_requests descriptive
        if computed_id and not computed_name:
            user_query = supabase.table("users").select("username").eq("user_id", str(computed_id)).single().execute()
            if user_query.data:
                computed_name = user_query.data.get("username")

        lab_request_record = {
            "request_uid": generated_request_uid,
            "patient_id": str(payload_data.get("patient_id")),
            "physician_id": str(computed_id) if computed_id else None, # Clean UUID mapping link
            "physician_name": computed_name,
            "test_type": payload_data.get("test_type").upper().replace(" ", "_"), 
            "clinical_notes": payload_data.get("clinical_notes"),
            "status": "PENDING_SAMPLE", 
            "encoded_by": CURRENT_ENCODER_ID
        }
        
        response = supabase.table("lab_requests").insert(lab_request_record).execute()
        return {
            "success": True,
            "request_id": generated_request_uid,
            "message": "Laboratory execution tracking pipeline initialized successfully.",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))