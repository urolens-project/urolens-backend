"""Specimen labeling request/response shapes; see `services/labeling_service.py`."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class ReceivedSpecimenSearchItem(BaseModel):
    """One `RECEIVED`-status specimen, for the label-generation search."""

    specimenId: UUID
    sampleUid: str | None = None
    patientName: str
    patientUid: str | None = None
    testType: str | None = None
    status: str


class LabelPreviewData(BaseModel):
    """The printable content of a generated specimen label."""

    patientName: str
    patientUid: str | None = None
    sampleUid: str | None = None
    testType: str | None = None
    date: str


class PrintLabelResponse(BaseModel):
    """Response body confirming a generated label and its print job."""

    success: bool
    labelId: UUID
    printJobId: UUID
    preview: LabelPreviewData


class LabelConfirmRequest(BaseModel):
    """Request body for confirming a label has been physically affixed."""

    offlineOverride: bool = False


class LabelConfirmResponse(BaseModel):
    """Response body confirming the specimen's advance to `LABELED` status."""

    success: bool
    message: str
    updatedStatus: str
    offlineOverrideUsed: bool
