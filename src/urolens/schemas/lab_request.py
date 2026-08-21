from __future__ import annotations

from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class LabRequestCreateRequest(BaseModel):
    patient_id: UUID
    physician_id: Optional[UUID] = None
    physician_name: Optional[str] = None
    test_type: str
    clinical_notes: Optional[str] = None


class LabRequestCreateResponse(BaseModel):
    success: bool
    request_id: str
    message: str
    timestamp: str


class PhysicianItem(BaseModel):
    user_id: UUID
    username: str
