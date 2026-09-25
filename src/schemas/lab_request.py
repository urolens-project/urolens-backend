"""Lab-request creation shapes, shared by the receptionist/encoder intake
flow and the physician-portal flow; see `services/lab_request_service.py`.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# Matches the widened lab_requests.test_type / specimens.test_type column
# width (migration 0035) — testType is now stored verbatim (see
# lab_request_service.create_lab_request), so this caps free-typed "Other"
# text with a clean 422 instead of a DB-level DataError on insert.
_TEST_TYPE_MAX_LENGTH = 255


class LabRequestCreateRequest(BaseModel):
    """Request body for creating a lab request.

    At least one of `physician_id`/`physician_name` is required — every lab
    request must have a requesting physician. If `physician_id` is given
    without a name, the name is looked up server-side.
    """

    patientId: UUID
    physicianId: UUID | None = None
    physicianName: str | None = None
    testType: str = Field(max_length=_TEST_TYPE_MAX_LENGTH)
    clinicalNotes: str | None = None
    specialInstructions: str | None = None

    @field_validator("testType")
    @classmethod
    def testTypeNotBlank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("physicianName")
    @classmethod
    def physicianNameTrimmed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None

    @model_validator(mode="after")
    def requirePhysicianIdentifier(self) -> LabRequestCreateRequest:
        if self.physicianId is None and not self.physicianName:
            raise ValueError("physicianId or physicianName is required")
        return self


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
    specialInstructions: str | None = None
    status: str
    createdAt: datetime


class PhysicianItem(BaseModel):
    """One physician, for populating a physician picker."""

    userId: UUID
    username: str
