from typing import Any, Optional

from pydantic import BaseModel


# ── MedTech: confirm result ────────────────────────────────────────────────
class ConfirmResultRequest(BaseModel):
    notes: Optional[str] = None


class ConfirmResultResponse(BaseModel):
    result_id: str
    status: str
    confirmed_at: str


# ── MedTech: manual override ───────────────────────────────────────────────
class OverrideParameterRequest(BaseModel):
    parameter_name: str
    original_ai_value: str
    corrected_value: str
    rationale: str


class OverrideParameterResponse(BaseModel):
    override_id: str
    result_id: str
    parameter_name: str


# ── Supervisor: pending queue ──────────────────────────────────────────────
class PendingResultItem(BaseModel):
    result_id: str
    specimen_id: str
    patient_name: str
    patient_age: Optional[int]
    patient_sex: Optional[str]
    medtech_name: str
    confirmed_at: Optional[str]
    status: str


class PendingResultListResponse(BaseModel):
    items: list[PendingResultItem]
    total: int
    page: int
    page_size: int


# ── Supervisor: full result detail ─────────────────────────────────────────
class ManualOverrideItem(BaseModel):
    override_id: str
    parameter_name: str
    original_ai_value: str
    corrected_value: str
    rationale: str
    overridden_at: str


class FullResultDetail(BaseModel):
    result_id: str
    specimen_id: str
    # patient
    patient_name: str
    patient_age: Optional[int]
    patient_sex: Optional[str]
    # medtech
    medtech_name: str
    confirmed_at: Optional[str]
    confirmation_notes: Optional[str]
    # AI findings
    ai_findings: dict[str, Any]
    flagged_anomalies: dict[str, Any]
    particle_classes: dict[str, Any]
    model_version: str
    # overrides
    manual_overrides: list[ManualOverrideItem]
    # image
    image_url: Optional[str]
    # smart diagnosis (may be None if WEB-09 not yet migrated)
    smart_diagnosis_unavailable: bool
    # workflow
    status: str
    # supervisor review
    annotation_notes: Optional[str]


# ── Supervisor: annotation ─────────────────────────────────────────────────
class AnnotationRequest(BaseModel):
    annotation_notes: str


class AnnotationResponse(BaseModel):
    result_id: str
    annotation_notes: str


# ── Supervisor: approve ────────────────────────────────────────────────────
class ApproveRequest(BaseModel):
    notes: Optional[str] = None


class ApproveResponse(BaseModel):
    result_id: str
    status: str
    approved_at: str


# ── Supervisor: return for correction ─────────────────────────────────────
class ReturnRequest(BaseModel):
    reason: str


class ReturnResponse(BaseModel):
    result_id: str
    status: str
    returned_at: str


# ── Supervisor: escalate ───────────────────────────────────────────────────
VALID_ESCALATION_PATHS = {"NOTIFY_PHYSICIAN", "FLAG_SENIOR_REVIEW", "MARK_CRITICAL"}


class EscalateRequest(BaseModel):
    escalation_path: str
    escalation_note: Optional[str] = None


class EscalateResponse(BaseModel):
    result_id: str
    status: str
    escalation_path: str
    escalated_at: str
