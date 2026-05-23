from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


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
