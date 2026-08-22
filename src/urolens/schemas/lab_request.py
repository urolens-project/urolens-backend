"""Lab-request creation shapes for the receptionist/encoder intake flow;
see `services/lab_request_service.py`."""
from __future__ import annotations

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
    """Response body confirming a created lab request."""

    success: bool
    request_id: str
    message: str
    timestamp: str


class PhysicianItem(BaseModel):
    """One physician, for populating a physician picker."""

    user_id: UUID
    username: str
