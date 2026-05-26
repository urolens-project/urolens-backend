from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ReleaseResultRequest(BaseModel):
    release_method: Literal["PHYSICAL", "DIGITAL"]


class ResultReleaseResponse(BaseModel):
    release_id: UUID
    result_id: UUID
    released_by: UUID
    release_method: str
    released_at: datetime


class ApprovedResultItem(BaseModel):
    result_id: UUID
    patient_name: str
    sample_uid: str | None
    test_type: str | None
    approved_at: datetime


class PaginationMeta(BaseModel):
    next_cursor: str | None
    has_more: bool


class ApprovedResultsResponse(BaseModel):
    data: list[ApprovedResultItem]
    pagination: PaginationMeta
