"""Request and response shapes for the multi-field workflow; see `api/fields.py`.

Boxes are percent (0 to 100) of the upright stored image, for AI and MedTech
boxes alike. Every shape here is camelCase on the wire.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..models.analysis_result import ResultStatus
from ..models.result_field import FieldStatus, Magnification
from .patient_portal import PARTICLE_LABELS

MAX_FIELD_COUNT = 1000
"""Largest corrected count for one class in one field. Matches the engine's
per-image detection cap."""

ParticleCounts = dict[str, float]
"""Class name to count. Integers per field; averages can be decimals."""

FieldFlagReason = Literal["LOW_CONFIDENCE", "OUTLIER_COUNT", "ABOVE_NORMAL_MAX"]
BlockingReason = Literal["FIELDS_BELOW_MINIMUM", "FIELDS_NOT_VERIFIED"]


def _checkParticleType(value: str) -> str:
    if value not in PARTICLE_LABELS:
        raise ValueError(f"particleType must be one of {PARTICLE_LABELS}")
    return value


# ── Boxes ────────────────────────────────────────────────────────────────────

class Detection(BaseModel):
    """One AI box. `id` is assigned by the backend and is stable within a field."""

    id: str
    particleType: str
    confidence: float
    x: float
    y: float
    w: float
    h: float


class AddedBox(BaseModel):
    """A box the MedTech drew."""

    id: str
    particleType: str
    x: float
    y: float
    w: float
    h: float


class AddedBoxInput(BaseModel):
    """A box to add, as sent by the client (the backend assigns the id)."""

    particleType: str
    x: float = Field(..., ge=0, le=100)
    y: float = Field(..., ge=0, le=100)
    w: float = Field(..., gt=0, le=100)
    h: float = Field(..., gt=0, le=100)

    @field_validator("particleType")
    @classmethod
    def particleTypeKnown(cls, v: str) -> str:
        """Reject a class that is not in `PARTICLE_LABELS`."""
        return _checkParticleType(v)


class FieldAnnotationEdits(BaseModel):
    """The current (not superseded) box edits on a field."""

    removedDetectionIds: list[str] = []
    addedBoxes: list[AddedBox] = []
    editedBy: uuid.UUID | None = None
    editedAt: datetime | None = None


# ── Fields ───────────────────────────────────────────────────────────────────

class FieldFlag(BaseModel):
    """Why a field is flagged for a closer look. Computed when read, not stored."""

    reason: FieldFlagReason
    particleType: str | None = None


class FieldOverride(BaseModel):
    """The override currently in force for one class of a field."""

    particleType: str
    originalAiValue: int
    correctedValue: int
    rationale: str
    overriddenBy: uuid.UUID
    overriddenAt: datetime


class FieldSummaryItem(BaseModel):
    """One row of `GET /results/{id}/fields`. Never carries detections."""

    fieldId: uuid.UUID
    resultId: uuid.UUID
    fieldNo: int
    magnification: Magnification
    status: FieldStatus
    imageUrl: str | None = None
    aiFindings: ParticleCounts
    effectiveCounts: ParticleCounts
    confidenceScores: dict[str, float]
    flags: list[FieldFlag] = []
    hasCorrections: bool = False
    overriddenClasses: list[str] = []
    """Classes whose count is set by an override; box editing is off for them."""
    verifiedBy: uuid.UUID | None = None
    verifiedAt: datetime | None = None
    discardReason: str | None = None
    version: int
    createdAt: datetime
    updatedAt: datetime


class FieldDetail(FieldSummaryItem):
    """`GET /fields/{field_id}`: one field in full. Opening a field this way is
    what lets the same user mark it as checked afterwards."""

    detections: list[Detection]
    imageWidth: int
    imageHeight: int
    annotationEdits: FieldAnnotationEdits
    overrides: list[FieldOverride] = []
    smartDiagnosisPreview: dict | None = None
    smartDiagnosisUnavailable: bool = False
    modelVersion: str


class FieldListResponse(BaseModel):
    """`GET /results/{id}/fields`."""

    resultId: uuid.UUID
    resultStatus: ResultStatus
    minFields: int
    acceptedFieldCount: int
    verifiedFieldCount: int
    undecidedFieldCount: int
    items: list[FieldSummaryItem]


# ── Requests ─────────────────────────────────────────────────────────────────

class SaveAnnotationsRequest(BaseModel):
    """`PUT /fields/{field_id}/annotations`: the full list of edits, replacing the
    previous list. A stale `expectedVersion` answers 409 `FIELD_VERSION_CONFLICT`
    and writes nothing."""

    removedDetectionIds: list[str] = []
    addedBoxes: list[AddedBoxInput] = []
    expectedVersion: int = Field(..., ge=1)


class FieldOverrideRequest(BaseModel):
    """`POST /fields/{field_id}/override`."""

    particleType: str
    correctedValue: int = Field(..., ge=0, le=MAX_FIELD_COUNT)
    rationale: str = Field(..., min_length=1, max_length=2000)

    @field_validator("particleType")
    @classmethod
    def particleTypeKnown(cls, v: str) -> str:
        """Reject a class that is not in `PARTICLE_LABELS`."""
        return _checkParticleType(v)

    @field_validator("rationale")
    @classmethod
    def rationaleNotBlank(cls, v: str) -> str:
        """Reject a blank reason; strip surrounding whitespace otherwise."""
        if not v.strip():
            raise ValueError("rationale must not be blank")
        return v.strip()


class RevertOverrideRequest(BaseModel):
    """`POST /fields/{field_id}/override/revert`."""

    particleType: str
    rationale: str = Field(..., min_length=1, max_length=2000)

    @field_validator("particleType")
    @classmethod
    def particleTypeKnown(cls, v: str) -> str:
        """Reject a class that is not in `PARTICLE_LABELS`."""
        return _checkParticleType(v)

    @field_validator("rationale")
    @classmethod
    def rationaleNotBlank(cls, v: str) -> str:
        """Reject a blank reason; strip surrounding whitespace otherwise."""
        if not v.strip():
            raise ValueError("rationale must not be blank")
        return v.strip()


class DiscardFieldRequest(BaseModel):
    """`POST /fields/{field_id}/discard`. The reason is optional on the mobile
    path and required on the web path (the service enforces which)."""

    reason: str | None = Field(default=None, max_length=2000)


# ── Responses to actions ─────────────────────────────────────────────────────

class FieldActionResponse(BaseModel):
    """Returned by accept, discard, annotations, override, revert and verify, so a
    client can update the field and the running totals in one go."""

    field: FieldSummaryItem
    acceptedFieldCount: int
    verifiedFieldCount: int
    undecidedFieldCount: int


class ResultStatusResponse(BaseModel):
    """Returned by submit and reopen-capture."""

    resultId: uuid.UUID
    status: ResultStatus
    updatedAt: datetime


# ── Aggregation and summary ──────────────────────────────────────────────────

class ParticleAggregate(BaseModel):
    """One class over the accepted fields."""

    mean: float
    min: float
    max: float
    total: float
    fieldsSeen: int
    aiMean: float
    displayValue: str
    """`"<1"` when 0 < mean < 0.5, else the rounded mean; shown as is, so a seen
    particle never prints as 0."""
    fieldsAboveNormal: list[int] = []
    """Field numbers where this class is above the Smart Diagnosis normal maximum."""


class SummaryCorrection(BaseModel):
    """One correction made during verification."""

    fieldId: uuid.UUID
    fieldNo: int
    particleType: str
    aiValue: int
    effectiveValue: int
    kind: Literal["BOXES", "OVERRIDE"]


class FocalFinding(BaseModel):
    """A class that is above its normal maximum in one or more fields even though
    the average may look normal."""

    particleType: str
    fieldNos: list[int]


class VerificationSummaryResponse(BaseModel):
    """`GET /results/{id}/summary`: the numbers confirmation will store."""

    resultId: uuid.UUID
    fieldCount: int
    minFields: int
    perClass: dict[str, ParticleAggregate]
    corrections: list[SummaryCorrection] = []
    focalFindings: list[FocalFinding] = []
    smartDiagnosisPreview: dict | None = None
    smartDiagnosisUnavailable: bool = False
    canConfirm: bool
    blockingReason: BlockingReason | None = None


# ── Shapes for routes the lanes will change (schema-only for now) ────────────

class FieldAnalysisResponse(BaseModel):
    """`POST /fields` (201): the field one image upload added to a specimen."""

    fieldId: uuid.UUID
    fieldNo: int
    magnification: Magnification
    resultId: uuid.UUID
    imageId: uuid.UUID
    status: FieldStatus
    aiFindings: ParticleCounts
    detections: list[Detection]
    imageWidth: int
    imageHeight: int
    confidenceScores: dict[str, float]
    smartDiagnosisPreview: dict | None = None
    smartDiagnosisUnavailable: bool = False
    acceptedFieldCount: int
    minFields: int


class MedtechPendingFieldCounts(BaseModel):
    """Additions to each item of `GET /results/medtech/pending`."""

    fieldCount: int
    verifiedFieldCount: int
    flaggedFieldCount: int


class FullResultFieldAdditions(BaseModel):
    """Additions to `GET /results/{result_id}`. `imageUrl` stays and points to the
    first accepted field for older readers."""

    fields: list[FieldSummaryItem]
    fieldCount: int
    fieldSummary: dict[str, ParticleAggregate] | None = None


class SyncResultField(BaseModel):
    """One field in `GET /sync/pull`. Explicit columns; never detections."""

    fieldId: uuid.UUID
    resultId: uuid.UUID
    specimenId: uuid.UUID
    imageId: uuid.UUID
    fieldNo: int
    magnification: Magnification
    status: FieldStatus
    aiFindings: ParticleCounts
    effectiveCounts: ParticleCounts
    confidenceScores: dict[str, float]
    imageWidth: int
    imageHeight: int
    smartDiagnosisPreview: dict | None = None
    """The field's own Smart Diagnosis preview, so a field captured on another
    phone (or before a reinstall) still shows it."""
    smartDiagnosisUnavailable: bool = False
    version: int
    createdAt: datetime
    updatedAt: datetime
