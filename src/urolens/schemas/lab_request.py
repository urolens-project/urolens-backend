"""Lab-request creation shapes, shared by the receptionist/encoder intake
flow and the physician-portal flow; see `services/lab_request_service.py`.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class LabRequestCreateRequest(BaseModel):
    """Request body for creating a lab request.

    `physician_id`/`physician_name` are both optional; if `physician_id` is
    given without a name, the name is looked up server-side.
    """

    patientId: UUID
    physicianId: UUID | None = None
    physicianName: str | None = None
    testType: str
    clinicalNotes: str | None = None


class LabRequestCreateResponse(BaseModel):
    """Response body confirming a created lab request — the one canonical
    shape returned by both the receptionist-facing and physician-facing
    creation routes (`domains/request/lab_requests_router.py` and
    `api/physician.py`), built directly from the persisted `LabRequest` row.
    """

    labRequestId: UUID
    requestUid: str
    patientId: UUID
    physicianId: UUID | None = None
    physicianName: str | None = None
    testType: str
    clinicalNotes: str | None = None
    status: str
    createdAt: datetime


class PhysicianItem(BaseModel):
    """One physician, for populating a physician picker."""

    userId: UUID
    username: str
