from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


# ── Mobile Developer schemas (do not modify) ──────────────────────────────────

class MedTechWorkload(BaseModel):
    medtech_id: UUID
    username: str
    queue_count: int


class QueueAssignRequest(BaseModel):
    specimen_id: UUID
    medtech_id: UUID


class QueueAssignResponse(BaseModel):
    assignment_id: UUID
    specimen_id: UUID
    medtech_id: UUID
    assigned_by: UUID
    assigned_at: datetime
    status: str


# ── Receptionist-facing schemas (STORY-WEB-08) ────────────────────────────────

class PendingSpecimenItem(BaseModel):
    specimen_id: UUID
    sample_uid: str
    patient_name: str
    test_type: str
    received_at: datetime
    status: str


class MedTechWorkloadItem(BaseModel):
    user_id: UUID
    full_name: str
    active_count: int


class AssignSpecimenRequest(BaseModel):
    specimen_id: UUID
    medtech_id: UUID
