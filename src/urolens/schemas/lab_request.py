"""Lab-request creation shapes, shared by the receptionist/encoder intake
flow and the physician-portal flow; see `services/lab_request_service.py`."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class LabRequestCreateRequest(BaseModel):
    """Request body for creating a lab request.

    `physician_id`/`physician_name` are both optional; if `physician_id` is
    given without a name, the name is looked up server-side.
    """

    patient_id: UUID
    physician_id: Optional[UUID] = None
    physician_name: Optional[str] = None
    test_type: str
    clinical_notes: Optional[str] = None


class LabRequestCreateResponse(BaseModel):
    """Response body confirming a created lab request — the one canonical
    shape returned by both the receptionist-facing and physician-facing
    creation routes (`domains/request/lab_requests_router.py` and
    `api/physician.py`), built directly from the persisted `LabRequest` row."""

    lab_request_id: UUID
    request_uid: str
    patient_id: UUID
    physician_id: Optional[UUID] = None
    physician_name: Optional[str] = None
    test_type: str
    clinical_notes: Optional[str] = None
    status: str
    created_at: datetime


class PhysicianItem(BaseModel):
    """One physician, for populating a physician picker."""

    user_id: UUID
    username: str
