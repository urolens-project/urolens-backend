from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CellCounts(BaseModel):
    rbc: int = 0
    wbc: int = 0
    epithelial_cells: int = 0
    casts: int = 0
    bacteria: int = 0
    crystals: int = 0
    mucus_threads: int = 0


class PatientResultSummary(BaseModel):
    result_id: UUID
    specimen_id: UUID
    status: str
    released_at: datetime | None
    created_at: datetime


class PatientResultDetail(BaseModel):
    result_id: UUID
    specimen_id: UUID
    patient_id: UUID | None = None
    status: str
    cell_counts: CellCounts | None
    interpretation: str | None
    medtech_name: str | None
    pathologist_name: str | None
    pathologist_license: str | None
    confirmed_at: datetime | None
    released_at: datetime | None
    created_at: datetime
