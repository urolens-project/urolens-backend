"""Specimen labeling request/response shapes; see `services/labeling_service.py`."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class ReceivedSpecimenSearchItem(BaseModel):
    """One `RECEIVED`-status specimen, for the label-generation search.

    No `patientName` — RA 10173 data-minimization: this list only needs to
    let the MedTech pick the right specimen by its non-PII identifiers. The
    label preview (`LabelPreviewData`, returned once a specimen is
    selected/generated) still carries `patientName`.
    """

    specimenId: UUID
    sampleUid: str | None = None
    patientUid: str | None = None
    testType: str | None = None
    status: str
    labelCount: int = 0


class LabelPreviewData(BaseModel):
    """The printable content of a generated specimen label."""

    patientName: str
    patientUid: str | None = None
    sampleUid: str | None = None
    testType: str | None = None
    date: str


class PrintLabelResponse(BaseModel):
    """Response body confirming a generated label. Generation no longer
    creates a print job (see `POST .../label/print`) — `printJobId` is kept
    here as `None` rather than removed, since older clients may still read it.
    """

    success: bool
    labelId: UUID
    printJobId: UUID | None = None
    preview: LabelPreviewData
    labelCount: int


class PrintJobResponse(BaseModel):
    """Response body confirming a print job created for a specimen's current label."""

    success: bool
    printJobId: UUID
    labelId: UUID
    status: str


class LabelConfirmRequest(BaseModel):
    """Request body for confirming a label has been physically affixed."""

    offlineOverride: bool = False


class LabelConfirmResponse(BaseModel):
    """Response body confirming the specimen's advance to `LABELED` status."""

    success: bool
    message: str
    updatedStatus: str
    offlineOverrideUsed: bool
