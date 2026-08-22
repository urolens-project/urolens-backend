"""Specimen labeling request/response shapes; see `services/labeling_service.py`."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class ReceivedSpecimenSearchItem(BaseModel):
    """One `RECEIVED`-status specimen, for the label-generation search."""

    specimen_id: UUID
    sample_uid: str | None = None
    patient_name: str
    patient_uid: str | None = None
    test_type: str | None = None
    status: str


class LabelPreviewData(BaseModel):
    """The printable content of a generated specimen label."""

    patient_name: str
    patient_uid: str | None = None
    sample_uid: str | None = None
    test_type: str | None = None
    date: str


class PrintLabelResponse(BaseModel):
    """Response body confirming a generated label and its print job."""

    success: bool
    label_id: UUID
    print_job_id: UUID
    preview: LabelPreviewData


class LabelConfirmRequest(BaseModel):
    """Request body for confirming a label has been physically affixed."""

    offline_override: bool = False


class LabelConfirmResponse(BaseModel):
    """Response body confirming the specimen's advance to `LABELED` status."""

    success: bool
    message: str
    updated_status: str
    offline_override_used: bool
