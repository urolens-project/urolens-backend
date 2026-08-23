"""Result-release request/response shapes; see `services/result_releasing_service.py`."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ReleaseResultRequest(BaseModel):
    """Request body for releasing an approved result."""

    releaseMethod: Literal["PHYSICAL", "DIGITAL"]


class ResultReleaseResponse(BaseModel):
    """Response body confirming a result release."""

    releaseId: UUID
    resultId: UUID
    releasedBy: UUID
    releaseMethod: str
    releasedAt: datetime


class ApprovedResultItem(BaseModel):
    """One approved-and-awaiting-release result, for the release queue list."""

    resultId: UUID
    patientName: str
    sampleUid: str | None
    testType: str | None
    approvedAt: datetime


class PaginationMeta(BaseModel):
    """Cursor-pagination metadata for a list response."""

    nextCursor: str | None
    hasMore: bool


class ApprovedResultsResponse(BaseModel):
    """Response body for the approved-results release queue."""

    data: list[ApprovedResultItem]
    pagination: PaginationMeta
