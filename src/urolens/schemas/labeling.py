from __future__ import annotations

from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ReceivedSpecimenSearchItem(BaseModel):
    specimen_id: UUID
    sample_uid: Optional[str] = None
    patient_name: str
    patient_uid: Optional[str] = None
    test_type: Optional[str] = None
    status: str


class LabelPreviewData(BaseModel):
    patient_name: str
    patient_uid: Optional[str] = None
    sample_uid: Optional[str] = None
    test_type: Optional[str] = None
    date: str


class PrintLabelResponse(BaseModel):
    success: bool
    label_id: UUID
    print_job_id: UUID
    preview: LabelPreviewData


class LabelConfirmRequest(BaseModel):
    offline_override: bool = False


class LabelConfirmResponse(BaseModel):
    success: bool
    message: str
    updated_status: str
    offline_override_used: bool
