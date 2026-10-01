"""Result confirm/override, supervisor review/approval, and Smart Diagnosis
lookup request/response shapes; see `services/result_confirmation_service.py`,
`services/manual_override_service.py`, and `services/result_review_service.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, field_validator

from .patient_portal import PARTICLE_LABELS

# The largest particle count an override may set (UROLENS-227). The AI engine
# reports at most 300 detections per image (YOLO's default `max_det`), and lab
# reporting tops out at ">100" per field (CLSI GP16), so anything larger is a
# typo (e.g. an extra zero), not a count. Raise it here if the lab asks.
MAX_OVERRIDE_COUNT = 300


class ConfirmResultResponse(BaseModel):
    """Response shape for a successful result confirmation."""

    id: UUID
    resultId: UUID
    confirmedBy: UUID
    confirmedAt: datetime
    resubmitted: bool = False
    """True when this re-confirms a result the supervisor returned for
    correction ("re-submitted for supervisor approval"), False for a first
    confirmation (UROLENS-226)."""
    status: str
    """The result's status after confirming — `PENDING_SUPERVISOR_APPROVAL`."""

    model_config = {"from_attributes": True}


class OverrideRequest(BaseModel):
    """Request body for overriding a single AI-generated result parameter."""

    parameter: str = Field(
        ...,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("parameter", "parameterName"),
    )
    """The AI findings key being corrected. Also accepted as `parameterName`: the
    web supervisor screen sends `parameter_name` (written for the May web-10
    endpoint), which its case-conversion bridge turns into `parameterName`
    (UROLENS-227). `parameter` wins if both are sent; responses always say
    `parameter`."""
    correctedValue: int = Field(..., ge=0, le=MAX_OVERRIDE_COUNT)
    """A particle count: a whole number from 0 to `MAX_OVERRIDE_COUNT`. Fractions
    (3.7) and non-finite values (Infinity) are rejected with a 422."""
    rationale: str = Field(..., min_length=1, max_length=2000)
    """Required (UROLENS-146/150): every correction must say why. Blank or
    whitespace-only is rejected; surrounding whitespace is stripped."""
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

    @field_validator("rationale")
    @classmethod
    def rationaleNoWhitespaceOnly(cls, v: str) -> str:
        """Reject a blank or whitespace-only `rationale`; strips surrounding whitespace otherwise."""
        if not v.strip():
            raise ValueError("rationale must not be blank")
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


MedtechQueueStatus = Literal["PENDING_CONFIRM", "RETURNED_FOR_CORRECTION"]
"""`status` filter values for the MedTech confirmation queue (UROLENS-225)."""

MedtechQueueSort = Literal["oldest", "newest"]
"""Queue order by when the specimen was received — mirrors the mobile queue's
Earliest/Latest filters. Returned-for-correction results always come first."""


class MedtechPendingResultItem(BaseModel):
    """One result awaiting this MedTech's confirmation (or re-confirmation,
    if a supervisor returned it), for the MedTech's own queue list.

    Identifies the patient by `patientUid` only — no name, matching the mobile
    app's privacy decision to show the patient code (UROLENS-225).
    """

    resultId: UUID
    specimenId: UUID
    sampleUid: str | None = None
    """The specimen's human-facing sample ID (e.g. `SMP-20260927-00012`)."""
    testType: str | None = None
    priorityLevel: str | None = None
    receivedAt: datetime | None = None
    patientUid: str = ""
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
    sampleUid: str | None = None
    """The specimen's human-facing sample ID (e.g. `SMP-20260927-00012`)."""
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
    sampleUid: str | None = None
    """The specimen's human-facing sample ID (e.g. `SMP-20260927-00012`)."""
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
    sampleUid: str | None = None
    """The specimen's human-facing sample ID (e.g. `SMP-20260927-00012`)."""
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
    overriddenBy: UUID
    overriddenByName: str
    """Resolved from `overriddenBy` via a batched `User` lookup; `""` if the
    user record can't be found (mirrors `medtechName`'s fallback elsewhere)."""


class SpatialAnnotationItem(BaseModel):
    """One point-plus-particle-type spatial annotation on a result's image.

    (UROLENS-149). `id` is client-supplied and stable across saves, so the
    frontend can remove or adjust a single annotation by resending the full
    list without it — `saveAnnotation` always replaces the whole list.
    """

    id: str = Field(..., min_length=1, max_length=64)
    x: float = Field(..., ge=0)
    y: float = Field(..., ge=0)
    particleType: str

    @field_validator("particleType")
    @classmethod
    def particleTypeMustBeKnown(cls, v: str) -> str:
        """Reject a `particleType` not in the canonical `PARTICLE_LABELS` set."""
        if v not in PARTICLE_LABELS:
            raise ValueError(f"particleType must be one of {PARTICLE_LABELS}")
        return v


class FullResultDetail(BaseModel):
    """Response body for the full single-result review/detail view.

    Serves the supervisor's review workspace and the MedTech's online review screen.
    """

    resultId: UUID
    specimenId: UUID
    sampleUid: str | None = None
    """The specimen's human-facing sample ID (e.g. `SMP-20260927-00012`)."""
    patientUid: str = ""
    patientName: str | None = None
    """`null` for a MedTech caller — they identify the patient by
    `patientUid` only (UROLENS-226); set for supervisors."""
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
    returnReason: str | None = None
    """The supervisor's latest reason, only while the result is
    RETURNED_FOR_CORRECTION (UROLENS-226)."""
    annotationNotes: str | None = None
    spatialAnnotations: list[SpatialAnnotationItem] | None = None
    """Persisted as of migration 0034 (JSONB); validated at this response
    boundary as of UROLENS-149 (previously an untyped list[dict])."""


class AnnotationRequest(BaseModel):
    """Request body for saving a supervisor's annotation on a result."""

    annotationNotes: str
    spatialAnnotations: list[SpatialAnnotationItem] | None = None


class AnnotationResponse(BaseModel):
    """Response body confirming a saved annotation."""

    resultId: UUID
    annotationNotes: str
    spatialAnnotations: list[SpatialAnnotationItem] | None = None


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

    reason: str = Field(..., min_length=1, max_length=2000)
    """Required: every return must say why. Blank or whitespace-only is
    rejected; surrounding whitespace is stripped (UROLENS-151)."""

    @field_validator("reason")
    @classmethod
    def reasonNoWhitespaceOnly(cls, v: str) -> str:
        """Reject a blank or whitespace-only `reason`; strips surrounding whitespace otherwise."""
        if not v.strip():
            raise ValueError("reason must not be blank")
        return v.strip()


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
