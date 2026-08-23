"""MedTech queue request/response shapes; see `services/queue_service.py`."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

# ── Mobile Developer schemas (do not modify) ──────────────────────────────────

class MedTechWorkload(BaseModel):
    """One MedTech's active queue-assignment count, for the workload list."""

    medtechId: UUID
    username: str
    queueCount: int


class QueueAssignRequest(BaseModel):
    """Request body for assigning a specimen to a MedTech."""

    specimenId: UUID
    medtechId: UUID


class QueueAssignResponse(BaseModel):
    """Response body confirming a created queue assignment."""

    assignmentId: UUID
    specimenId: UUID
    medtechId: UUID
    assignedBy: UUID
    assignedAt: datetime
    status: str


# ── Receptionist-facing schemas (STORY-WEB-08) ────────────────────────────────

class PendingSpecimenItem(BaseModel):
    """One `LABELED` specimen awaiting assignment, for the receptionist queue view."""

    specimenId: UUID
    sampleUid: str
    patientName: str
    testType: str
    receivedAt: datetime
    status: str


class MedTechWorkloadItem(BaseModel):
    """One MedTech's active specimen count, for the receptionist workload view."""

    userId: UUID
    fullName: str
    activeCount: int


class AssignSpecimenRequest(BaseModel):
    """Request body for assigning a specimen to a MedTech (receptionist-facing)."""

    specimenId: UUID
    medtechId: UUID
