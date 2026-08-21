from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class SpecimenReceiveRequest(BaseModel):
    lab_request_id: UUID
    visual_check_passed: bool
    rejection_reason: Optional[str] = None
    free_text_note: Optional[str] = None


class SpecimenReceiveResponse(BaseModel):
    success: bool
    specimen_id: UUID
    sample_uid: Optional[str] = None
    status: str
    message: str


class SpecimenListItem(BaseModel):
    specimen_id: UUID
    lab_request_id: UUID
    sample_uid: Optional[str] = None
    status: str
    patient_name: Optional[str] = None
    patient_uid: Optional[str] = None
    test_type: Optional[str] = None
    priority_level: str
    received_at: datetime


class LabRequestSearchItem(BaseModel):
    lab_request_id: UUID
    request_uid: str
    test_type: str
    physician_name: Optional[str] = None
    patient_id: UUID


class SpecimenRejectRequest(BaseModel):
    reason_code: str
    free_text_note: Optional[str] = None


class SpecimenRejectResponse(BaseModel):
    specimen_id: UUID
    status: str
    rejected_at: str
