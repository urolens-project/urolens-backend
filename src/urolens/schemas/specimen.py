"""Specimen intake request/response shapes; see `services/specimen_service.py`."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SpecimenReceiveRequest(BaseModel):
    """Request body for receiving a specimen against a lab request."""

    lab_request_id: UUID
    visual_check_passed: bool
    rejection_reason: str | None = None
    free_text_note: str | None = None


class SpecimenReceiveResponse(BaseModel):
    """Response body confirming a received (or rejected) specimen."""

    success: bool
    specimen_id: UUID
    sample_uid: str | None = None
    status: str
    message: str


class SpecimenListItem(BaseModel):
    """One specimen, for the specimen listing view."""

    specimen_id: UUID
    lab_request_id: UUID
    sample_uid: str | None = None
    status: str
    patient_name: str | None = None
    patient_uid: str | None = None
    test_type: str | None = None
    priority_level: str
    received_at: datetime


class LabRequestSearchItem(BaseModel):
    """One `PENDING_SAMPLE` lab request, for the specimen-receiving search."""

    lab_request_id: UUID
    request_uid: str
    test_type: str
    physician_name: str | None = None
    patient_id: UUID


class SpecimenRejectRequest(BaseModel):
    """Request body for a MedTech's post-assignment specimen rejection."""

    reason_code: str
    free_text_note: str | None = None


class SpecimenRejectResponse(BaseModel):
    """Response body confirming a specimen rejection."""

    specimen_id: UUID
    status: str
    rejected_at: str
