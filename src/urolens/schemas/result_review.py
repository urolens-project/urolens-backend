from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel


class SupervisorStatsResponse(BaseModel):
    pendingCount: int
    approvedToday: int
    escalatedCount: int


class PendingResultItem(BaseModel):
    result_id: UUID
    specimen_id: UUID
    patient_name: str
    patient_age: Optional[int] = None
    patient_sex: Optional[str] = None
    medtech_name: str
    confirmed_at: Optional[datetime] = None
    status: str


class PendingResultListResponse(BaseModel):
    items: List[PendingResultItem]
    total: int
    page: int
    page_size: int


class ApprovedResultItem(BaseModel):
    result_id: UUID
    specimen_id: UUID
    patient_name: str
    patient_age: Optional[int] = None
    patient_sex: Optional[str] = None
    medtech_name: str
    approved_at: datetime
    status: str


class ApprovedTodayListResponse(BaseModel):
    items: List[ApprovedResultItem]
    total: int
    page: int
    page_size: int


class EscalatedResultItem(BaseModel):
    result_id: UUID
    specimen_id: UUID
    patient_name: str
    patient_age: Optional[int] = None
    patient_sex: Optional[str] = None
    medtech_name: str
    escalated_at: Optional[datetime] = None
    escalation_path: str
    status: str


class EscalatedListResponse(BaseModel):
    items: List[EscalatedResultItem]
    total: int
    page: int
    page_size: int


class ManualOverrideItem(BaseModel):
    override_id: UUID
    parameter_name: str
    original_ai_value: str
    corrected_value: str
    rationale: str
    overridden_at: datetime


class FullResultDetail(BaseModel):
    result_id: UUID
    specimen_id: UUID
    patient_name: str
    patient_age: Optional[int] = None
    patient_sex: Optional[str] = None
    medtech_name: str
    confirmed_at: Optional[datetime] = None
    confirmation_notes: Optional[str] = None
    """Always None in the ported service — analysis_results.confirmation_notes
    has no Alembic history (schema-drift finding, not modeled). Kept in the
    response shape for API-contract compatibility."""
    ai_findings: Dict[str, Any]
    flagged_anomalies: Dict[str, Any]
    particle_classes: Dict[str, Any]
    model_version: str
    manual_overrides: List[ManualOverrideItem]
    image_url: Optional[str] = None
    smart_diagnosis_unavailable: bool
    status: str
    annotation_notes: Optional[str] = None
    spatial_annotations: Optional[List[Dict[str, Any]]] = None
    """Persisted as of migration 0034 (JSONB) — type inferred from pre-port
    code, not yet verified against a live database. See the ResultReview
    model's docstring."""


class AnnotationRequest(BaseModel):
    annotation_notes: str
    spatial_annotations: Optional[List[Dict[str, Any]]] = None


class AnnotationResponse(BaseModel):
    result_id: UUID
    annotation_notes: str
    spatial_annotations: Optional[List[Dict[str, Any]]] = None


class ApproveRequest(BaseModel):
    notes: Optional[str] = None


class ApproveResponse(BaseModel):
    result_id: UUID
    status: str
    approved_at: datetime


class ReturnRequest(BaseModel):
    reason: str


class ReturnResponse(BaseModel):
    result_id: UUID
    status: str
    returned_at: datetime


VALID_ESCALATION_PATHS = {"NOTIFY_PHYSICIAN", "FLAG_SENIOR_REVIEW", "MARK_CRITICAL"}


class EscalateRequest(BaseModel):
    escalation_path: str
    escalation_note: Optional[str] = None


class EscalateResponse(BaseModel):
    result_id: UUID
    status: str
    escalation_path: str
    escalated_at: datetime
