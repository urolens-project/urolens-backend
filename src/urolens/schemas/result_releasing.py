"""Result-release request/response shapes; see `services/result_releasing_service.py`."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ReleaseResultRequest(BaseModel):
    """Request body for releasing an approved result."""

    release_method: Literal["PHYSICAL", "DIGITAL"]


class ResultReleaseResponse(BaseModel):
    """Response body confirming a result release."""

    release_id: UUID
    result_id: UUID
    released_by: UUID
    release_method: str
    released_at: datetime


class ApprovedResultItem(BaseModel):
    """One approved-and-awaiting-release result, for the release queue list."""

    result_id: UUID
    patient_name: str
    sample_uid: str | None
    test_type: str | None
    approved_at: datetime


class PaginationMeta(BaseModel):
    """Cursor-pagination metadata for a list response."""

    next_cursor: str | None
    has_more: bool


class ApprovedResultsResponse(BaseModel):
    """Response body for the approved-results release queue."""

    data: list[ApprovedResultItem]
    pagination: PaginationMeta
