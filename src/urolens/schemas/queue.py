"""MedTech queue request/response shapes; see `services/queue_service.py`."""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


# ── Mobile Developer schemas (do not modify) ──────────────────────────────────

class MedTechWorkload(BaseModel):
    """One MedTech's active queue-assignment count, for the workload list."""

    medtech_id: UUID
    username: str
    queue_count: int


class QueueAssignRequest(BaseModel):
    """Request body for assigning a specimen to a MedTech."""

    specimen_id: UUID
    medtech_id: UUID


class QueueAssignResponse(BaseModel):
    """Response body confirming a created queue assignment."""

    assignment_id: UUID
    specimen_id: UUID
    medtech_id: UUID
    assigned_by: UUID
    assigned_at: datetime
    status: str


# ── Receptionist-facing schemas (STORY-WEB-08) ────────────────────────────────

class PendingSpecimenItem(BaseModel):
    """One `LABELED` specimen awaiting assignment, for the receptionist queue view."""

    specimen_id: UUID
    sample_uid: str
    patient_name: str
    test_type: str
    received_at: datetime
    status: str


class MedTechWorkloadItem(BaseModel):
    """One MedTech's active specimen count, for the receptionist workload view."""

    user_id: UUID
    full_name: str
    active_count: int


class AssignSpecimenRequest(BaseModel):
    """Request body for assigning a specimen to a MedTech (receptionist-facing)."""

    specimen_id: UUID
    medtech_id: UUID
