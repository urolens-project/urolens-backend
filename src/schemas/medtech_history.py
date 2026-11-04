"""MedTech sample history shapes (UROLENS-236); see `services/medtech_history_service.py`."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

MedtechHistoryCategory = Literal["PENDING_APPROVAL", "APPROVED", "RELEASED", "REJECTED"]
"""The mobile Reports categories: a result with the supervisor, approved or
released, or a specimen the MedTech rejected."""


class MedtechHistoryItem(BaseModel):
    """One finished (or submitted) sample in the MedTech's history.

    Identifies the patient by `patientUid` only — no name, matching the mobile
    app's privacy decision to show the patient code (UROLENS-225).
    """

    specimenId: UUID
    resultId: UUID | None = None
    """`None` for a specimen rejected before it had a result."""
    sampleUid: str | None = None
    patientUid: str | None = None
    testType: str | None = None
    priorityLevel: str | None = None
    receivedAt: datetime | None = None
    category: MedtechHistoryCategory
    finalizedAt: datetime | None = None
    """When the sample reached its category: confirmed (pending approval),
    approved, released, or rejected."""
    rejectionReason: str | None = None


class MedtechHistoryListResponse(BaseModel):
    """Response body for the MedTech's paginated sample history."""

    items: list[MedtechHistoryItem]
    total: int
    page: int
    pageSize: int
