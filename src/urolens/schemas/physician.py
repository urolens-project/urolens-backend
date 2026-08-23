"""Physician-portal patient search, lab-request creation, and result
listing/detail shapes; see `services/physician_service.py` and
`services/physician_result_service.py`.
"""
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class PhysicianPatientItem(BaseModel):
    """One patient, for the physician's patient-search results (decrypted)."""

    patientId: UUID
    patientUid: str
    firstName: str
    middleName: str | None = None
    lastName: str
    dateOfBirth: str
    sex: str


class LabRequestCreateRequest(BaseModel):
    """Request body for a physician creating a lab request. Unlike
    `schemas.lab_request.LabRequestCreateRequest`, the physician is implicit
    (from the authenticated caller), so there's no `physician_id` field.
    """

    patientId: UUID
    testType: str
    clinicalNotes: str | None = None


# The response schema for lab-request creation lives in schemas/lab_request.py
# (LabRequestCreateResponse) — shared with the receptionist-facing route
# since both now return the same shape (see changelog.md's "Duplicate
# lab-request creation implementations" entry).


class PhysicianResultSummary(BaseModel):
    """One result, for the physician's result list."""

    resultId: str
    specimenId: str
    patientName: str
    patientUid: str
    patientAge: int | None
    patientSex: str | None
    status: str
    confirmedAt: str | None
    createdAt: str


class PhysicianResultListResponse(BaseModel):
    """Response body for the physician's paginated result list."""

    items: list[PhysicianResultSummary]
    total: int
    page: int
    pageSize: int


class SmartDiagnosisDetail(BaseModel):
    """Smart Diagnosis output, as shown in the physician result-detail view."""

    goutScore: str
    gnScore: str
    nephroScore: str
    utiScore: str
    trichoScore: str
    evidenceMap: dict[str, Any]
    noSignificantIndicators: bool
    engineVersion: str


class PhysicianResultDetail(BaseModel):
    """Response body for the physician's single-result detail view."""

    resultId: str
    specimenId: str
    patientName: str
    patientUid: str
    patientAge: int | None
    patientSex: str | None
    medtechName: str | None
    confirmedAt: str | None
    aiFindings: dict[str, Any]
    flaggedAnomalies: dict[str, Any]
    particleClasses: dict[str, Any]
    modelVersion: str
    smartDiagnosis: SmartDiagnosisDetail | None
    imageUrl: str | None
    status: str
    annotationNotes: str | None
