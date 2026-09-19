"""Result confirm/override, supervisor review/approval, and Smart Diagnosis
lookup request/response shapes; see `services/result_confirmation_service.py`,
`services/manual_override_service.py`, and `services/result_review_service.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ConfirmResultResponse(BaseModel):
    """Response shape for a successful result confirmation."""

    id: UUID
    resultId: UUID
    confirmedBy: UUID
    confirmedAt: datetime

    model_config = {"from_attributes": True}


class OverrideRequest(BaseModel):
    """Request body for overriding a single AI-generated result parameter."""

    parameter: str = Field(..., min_length=1, max_length=100)
    correctedValue: float = Field(..., ge=0)
    rationale: str = Field("No rationale provided", max_length=2000)
    """Not Optional despite having a default: `ManualOverride.rationale` is a
    NOT NULL column, so an explicit `"rationale": null` in the request must
    be rejected by Pydantic (a clean 422) rather than reach the service and
    fail as an unhandled IntegrityError."""
    originalAiValue: float | None = Field(
        None,
        ge=0,
        description=(
            "Accepted for API-contract compatibility but ignored — the service "
            "always re-derives the original value from the stored ai_findings."
        ),
    )

    @field_validator("parameter")
    @classmethod
    def parameterNoWhitespaceOnly(cls, v: str) -> str:
        """Reject a `parameter` that is blank or whitespace-only; strips
        surrounding whitespace otherwise.
        """
        if not v.strip():
            raise ValueError("parameter must not be blank")
        return v.strip()


class OverrideResponse(BaseModel):
    """Response shape for a successful parameter override."""

    id: UUID
    resultId: UUID
    parameter: str
    originalAiValue: float
    correctedValue: float
    rationale: str
    overriddenBy: UUID
    overriddenAt: datetime

    model_config = {"from_attributes": True}


class SupervisorStatsResponse(BaseModel):
    """Response body for the supervisor dashboard's summary counts."""

    pendingCount: int
    approvedToday: int
    escalatedCount: int


class MedtechPendingResultItem(BaseModel):
    """One result awaiting this MedTech's confirmation (or re-confirmation,
    if a supervisor returned it), for the MedTech's own queue list.
    """

    resultId: UUID
    specimenId: UUID
    patientUid: str = ""
    patientName: str
    patientAge: int | None = None
    patientSex: str | None = None
    status: str
    returnReason: str | None = None
    """The supervisor's reason, present only when status is
    RETURNED_FOR_CORRECTION."""


class MedtechPendingListResponse(BaseModel):
    """Response body for the MedTech's paginated confirmation queue."""

    items: list[MedtechPendingResultItem]
    total: int
    page: int
    pageSize: int


class PendingResultItem(BaseModel):
    """One result awaiting supervisor approval, for the pending queue list."""

    resultId: UUID
    specimenId: UUID
    patientUid: str = ""
    patientName: str
    patientAge: int | None = None
    patientSex: str | None = None
    medtechName: str
    confirmedAt: datetime | None = None
    status: str


class PendingResultListResponse(BaseModel):
    """Response body for the supervisor's paginated pending queue."""

    items: list[PendingResultItem]
    total: int
    page: int
    pageSize: int


class ApprovedResultItem(BaseModel):
    """One result approved today, for the supervisor's approved-today list.
    Distinct from `schemas.result_releasing.ApprovedResultItem`, which serves
    the receptionist release queue.
    """

    resultId: UUID
    specimenId: UUID
    patientUid: str = ""
    patientName: str
    patientAge: int | None = None
    patientSex: str | None = None
    medtechName: str
    approvedAt: datetime
    status: str


class ApprovedTodayListResponse(BaseModel):
    """Response body for the supervisor's paginated approved-today list."""

    items: list[ApprovedResultItem]
    total: int
    page: int
    pageSize: int


class EscalatedResultItem(BaseModel):
    """One escalated result, for the supervisor's escalated-results list."""

    resultId: UUID
    specimenId: UUID
    patientUid: str = ""
    patientName: str
    patientAge: int | None = None
    patientSex: str | None = None
    medtechName: str
    escalatedAt: datetime | None = None
    escalationPath: str
    status: str


class EscalatedListResponse(BaseModel):
    """Response body for the supervisor's paginated escalated-results list."""

    items: list[EscalatedResultItem]
    total: int
    page: int
    pageSize: int


class ManualOverrideItem(BaseModel):
    """One MedTech parameter override, as shown in a result's full detail view."""

    overrideId: UUID
    parameterName: str
    originalAiValue: str
    correctedValue: str
    rationale: str
    overriddenAt: datetime


class FullResultDetail(BaseModel):
    """Response body for the supervisor's full single-result review/detail view."""

    resultId: UUID
    specimenId: UUID
    patientUid: str = ""
    patientName: str
    patientAge: int | None = None
    patientSex: str | None = None
    medtechName: str
    confirmedAt: datetime | None = None
    confirmationNotes: str | None = None
    """Always None in the ported service — analysis_results.confirmation_notes
    has no Alembic history (schema-drift finding, not modeled). Kept in the
    response shape for API-contract compatibility."""
    aiFindings: dict[str, Any]
    flaggedAnomalies: dict[str, Any]
    particleClasses: dict[str, Any]
    modelVersion: str
    manualOverrides: list[ManualOverrideItem]
    imageUrl: str | None = None
    smartDiagnosis: dict[str, Any] | None = None
    smartDiagnosisUnavailable: bool
    status: str
    annotationNotes: str | None = None
    spatialAnnotations: list[dict[str, Any]] | None = None
    """Persisted as of migration 0034 (JSONB) — type inferred from pre-port
    code, not yet verified against a live database. See the ResultReview
    model's docstring."""


class AnnotationRequest(BaseModel):
    """Request body for saving a supervisor's annotation on a result."""

    annotationNotes: str
    spatialAnnotations: list[dict[str, Any]] | None = None


class AnnotationResponse(BaseModel):
    """Response body confirming a saved annotation."""

    resultId: UUID
    annotationNotes: str
    spatialAnnotations: list[dict[str, Any]] | None = None


class ApproveRequest(BaseModel):
    """Request body for approving a pending result."""

    notes: str | None = None


class ApproveResponse(BaseModel):
    """Response body confirming a result approval."""

    resultId: UUID
    status: str
    approvedAt: datetime


class ReturnRequest(BaseModel):
    """Request body for returning a pending result for correction."""

    reason: str


class ReturnResponse(BaseModel):
    """Response body confirming a result return."""

    resultId: UUID
    status: str
    returnedAt: datetime


EscalationPath = Literal["NOTIFY_PHYSICIAN", "FLAG_SENIOR_REVIEW", "MARK_CRITICAL"]
"""The valid values for a result escalation path. Single source of truth for
both `EscalateRequest`'s field type (rejected by Pydantic at the request
boundary) and `VALID_ESCALATION_PATHS` (the runtime set
`ResultReviewService.escalate_result` checks against) — previously two
independent, duplicate definitions; see changelog.md's "Duplicate
VALID_ESCALATION_PATHS constant" entry."""

VALID_ESCALATION_PATHS = set(get_args(EscalationPath))


class EscalateRequest(BaseModel):
    """Request body for escalating a pending result."""

    escalationPath: EscalationPath
    escalationNote: str | None = None


class EscalateResponse(BaseModel):
    """Response body confirming a result escalation."""

    resultId: UUID
    status: str
    escalationPath: str
    escalatedAt: datetime


# ── Smart Diagnosis lookup (plan: neither row 6 nor row 7) ─────────────────────
# Folded in from app/schemas/results.py, unchanged.

ProbabilityLevel = Literal["LOW", "MODERATE", "HIGH"]
"""The three risk levels a Smart Diagnosis condition score can take."""


class EvidenceMap(BaseModel):
    """Per-condition list of contributing evidence particle names.

    Unused by `get_smart_diagnosis` itself, which returns a raw
    `dict[str, Any]` for `evidence_map` rather than this shape — kept for
    API-contract compatibility from the pre-port schema.
    """

    gout: list[str] = []
    uti: list[str] = []
    tricho: list[str] = []


class SmartDiagnosisAttached(BaseModel):
    """Response shape for a result with an attached Smart Diagnosis output."""

    outputId: str
    resultId: str
    status: Literal["ATTACHED"]
    goutScore: ProbabilityLevel
    gnScore: ProbabilityLevel
    nephroScore: ProbabilityLevel
    evidenceMap: dict[str, Any]
    noSignificantIndicators: bool
    engineVersion: str
    generatedAt: str


class SmartDiagnosisUnavailable(BaseModel):
    """Response shape for a result with no usable Smart Diagnosis output."""

    resultId: str
    status: Literal["FLAGGED_UNAVAILABLE"]


SmartDiagnosisResponse = SmartDiagnosisAttached | SmartDiagnosisUnavailable
"""Response model for `GET /{result_id}/smart-diagnosis` — one of the two
shapes above, discriminated by `status`."""
