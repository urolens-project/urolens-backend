"""Specimen intake request/response shapes; see `services/specimen_service.py`."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SpecimenReceiveRequest(BaseModel):
    """Request body for receiving a specimen against a lab request."""

    labRequestId: UUID
    visualCheckPassed: bool
    rejectionReason: str | None = None
    freeTextNote: str | None = None


class SpecimenReceiveResponse(BaseModel):
    """Response body confirming a received (or rejected) specimen."""

    success: bool
    specimenId: UUID
    sampleUid: str | None = None
    status: str
    message: str


class SpecimenListItem(BaseModel):
    """One specimen, for the specimen listing view."""

    specimenId: UUID
    labRequestId: UUID
    sampleUid: str | None = None
    status: str
    patientName: str | None = None
    patientUid: str | None = None
    testType: str | None = None
    priorityLevel: str
    receivedAt: datetime


class LabRequestSearchItem(BaseModel):
    """One `PENDING_SAMPLE` lab request, for the specimen-receiving search."""

    labRequestId: UUID
    requestUid: str
    testType: str
    physicianName: str | None = None
    patientId: UUID


class SpecimenRejectRequest(BaseModel):
    """Request body for a MedTech's post-assignment specimen rejection."""

    reasonCode: str
    freeTextNote: str | None = None


class SpecimenRejectResponse(BaseModel):
    """Response body confirming a specimen rejection."""

    specimenId: UUID
    status: str
    rejectedAt: str
