"""Pydantic schemas for the result-confirmation endpoints.

STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/app/schemas/result_confirmation.py

Single Responsibility: request/response serialisation only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ── Request ────────────────────────────────────────────────────────────────────

class ConfirmResultRequest(BaseModel):
    """Body for POST /api/v1/results/{id}/confirm."""

    notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional MedTech notes at time of confirmation.",
    )


# ── Response sub-schemas ───────────────────────────────────────────────────────

class AIFinding(BaseModel):
    parameter: str
    value: float
    unit: str
    is_flagged: bool
    reference_range: str | None = None


class SmartDiagnosisCondition(BaseModel):
    condition: str
    level: str          # e.g. "HIGH", "MODERATE", "LOW"
    confidence: float
    supporting_params: list[str]


class SmartDiagnosisOut(BaseModel):
    conditions: list[SmartDiagnosisCondition]
    generated_at: datetime | None = None
    disclaimer: str = (
        "Smart Diagnosis is an AI-assisted decision-support tool. "
        "Results must be reviewed and confirmed by a qualified Medical Technologist."
    )


class ResultConfirmationOut(BaseModel):
    """Minimal confirmation receipt returned after POST confirm."""

    id: uuid.UUID
    result_id: uuid.UUID
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime
    status: str
    smart_diagnosis_triggered: bool

    model_config = {"from_attributes": True}


# ── Full result response (GET /api/v1/results/{id}) ───────────────────────────

class FullResultOut(BaseModel):
    """Complete analysis result including AI findings and Smart Diagnosis.

    TASK-MOB-09-5 response shape.
    """

    id: uuid.UUID
    specimen_id: uuid.UUID
    status: str
    ai_findings: list[AIFinding]
    smart_diagnosis: SmartDiagnosisOut | None = None
    confirmation: ResultConfirmationOut | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}