from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import random
import uuid
import logging

from app.middleware.rbac import RequireRole
from src.urolens.core.database import supabase
from src.urolens.core.enums import UserRole

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/lab-requests",
    tags=["Lab Requests"]
)

_receptionist = RequireRole([UserRole.RECEPTIONIST])


class LabRequestCreatePayload(BaseModel):
    patient_id: uuid.UUID
    physician_id: Optional[uuid.UUID] = None # Change this from name to an optional UUID!
    physician_name: Optional[str] = None     # Used as a fallback if manual entry is toggled
    test_type: str
    clinical_notes: Optional[str] = None

@router.get("/physicians", status_code=status.HTTP_200_OK)
async def get_physicians_endpoint(current_user: dict = Depends(_receptionist)):
    try:
        response = await supabase.table("users")\
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
async def create_lab_request_endpoint(
    payload: LabRequestCreatePayload,
    current_user: dict = Depends(_receptionist),
):
    try:
        encoder_id = current_user["user_id"]
        generated_request_uid = f"REQ-2026-{random.randint(10000, 99999)}"
        payload_data = payload.dict()

        computed_id = payload_data.get("physician_id")
        computed_name = payload_data.get("physician_name")

        if computed_id and not computed_name:
            user_query = await supabase.table("users")\
                .select("username")\
                .eq("user_id", str(computed_id))\
                .single()\
                .execute()
            if user_query.data:
                computed_name = user_query.data.get("username")

        lab_request_record = {
            "request_uid": generated_request_uid,
            "patient_id": str(payload_data.get("patient_id")),
            "physician_id": str(computed_id) if computed_id else None,
            "physician_name": computed_name,
            "test_type": payload_data.get("test_type").upper().replace(" ", "_"),
            "clinical_notes": payload_data.get("clinical_notes"),
            "status": "PENDING_SAMPLE",
            "encoded_by": encoder_id
        }

        response = await supabase.table("lab_requests").insert(lab_request_record).execute()
        return {
            "success": True,
            "request_id": generated_request_uid,
            "message": "Laboratory execution tracking pipeline initialized successfully.",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
