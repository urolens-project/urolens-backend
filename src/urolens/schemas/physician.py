"""Physician-portal patient search, lab-request creation, and result
listing/detail shapes; see `services/physician_service.py` and
`services/physician_result_service.py`.
"""
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class PhysicianPatientItem(BaseModel):
    """One patient, for the physician's patient-search results (decrypted)."""

    patient_id: UUID
    patient_uid: str
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: str
    sex: str


class LabRequestCreateRequest(BaseModel):
    """Request body for a physician creating a lab request. Unlike
    `schemas.lab_request.LabRequestCreateRequest`, the physician is implicit
    (from the authenticated caller), so there's no `physician_id` field.
    """

    patient_id: UUID
    test_type: str
    clinical_notes: str | None = None


# The response schema for lab-request creation lives in schemas/lab_request.py
# (LabRequestCreateResponse) — shared with the receptionist-facing route
# since both now return the same shape (see changelog.md's "Duplicate
# lab-request creation implementations" entry).


class PhysicianResultSummary(BaseModel):
    """One result, for the physician's result list."""

    result_id: str
    specimen_id: str
    patient_name: str
    patient_uid: str
    patient_age: int | None
    patient_sex: str | None
    status: str
    confirmed_at: str | None
    created_at: str


class PhysicianResultListResponse(BaseModel):
    """Response body for the physician's paginated result list."""

    items: list[PhysicianResultSummary]
    total: int
    page: int
    page_size: int


class SmartDiagnosisDetail(BaseModel):
    """Smart Diagnosis output, as shown in the physician result-detail view."""

    gout_score: str
    gn_score: str
    nephro_score: str
    uti_score: str
    tricho_score: str
    evidence_map: dict[str, Any]
    no_significant_indicators: bool
    engine_version: str


class PhysicianResultDetail(BaseModel):
    """Response body for the physician's single-result detail view."""

    result_id: str
    specimen_id: str
    patient_name: str
    patient_uid: str
    patient_age: int | None
    patient_sex: str | None
    medtech_name: str | None
    confirmed_at: str | None
    ai_findings: dict[str, Any]
    flagged_anomalies: dict[str, Any]
    particle_classes: dict[str, Any]
    model_version: str
    smart_diagnosis: SmartDiagnosisDetail | None
    image_url: str | None
    status: str
    annotation_notes: str | None
