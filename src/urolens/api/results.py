"""
Result confirm/override routes (consolidation plan row 6).

Scope note: this file previously also contained supervisor-review routes
(`/pending`, `/{id}/full`, `/annotate`, `/approve`, `/return`, `/escalate`)
bridged directly to the old, unported `app.services.result_review_service`,
plus two generic result-viewing routes (`GET /{id}`, `GET /{id}/smart-diagnosis`)
backed by inline Supabase calls. All of that was removed before this file was
mounted — every one of those paths is still live today via `app/api/results.py`
(row 7, untouched, out of scope for this task), and mounting both versions
side by side at the same `/api/v1/results` prefix would have silently shadowed
one router's routes with the other's, in registration order, with no error.
See docs/backend-consolidation-plan.md row 6's note and CHANGELOG.md for the
full explanation — the removed route definitions are recoverable from git
history if useful when row 7 is actually ported.
"""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.rbac import RequireRole

from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.enums import UserRole
from ..services.manual_override_service import ManualOverrideService
from ..services.notification_service import NotificationService
from ..services.result_confirmation_service import ResultConfirmationService
from ..services.smart_diagnosis_service import SmartDiagnosisService

router = APIRouter(prefix="/api/v1/results", tags=["results"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ConfirmResultResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    confirmed_by: uuid.UUID
    confirmed_at: datetime

    model_config = {"from_attributes": True}


class OverrideRequest(BaseModel):
    parameter: str = Field(..., min_length=1, max_length=100)
    corrected_value: float = Field(..., ge=0)
    rationale: Optional[str] = Field("No rationale provided", max_length=2000)
    original_ai_value: float = Field(..., ge=0)

    @field_validator("parameter")
    @classmethod
    def parameter_no_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("parameter must not be blank")
        return v.strip()


class OverrideResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    parameter: str
    original_ai_value: float
    corrected_value: float
    rationale: str
    overridden_by: uuid.UUID
    overridden_at: datetime

    model_config = {"from_attributes": True}


# ── Dependency factories (DIP) ────────────────────────────────────────────────

async def get_notif_service(
    db: AsyncSession = Depends(get_db),
) -> NotificationService:
    return NotificationService(db=db)


async def get_confirmation_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
    notif_service: NotificationService = Depends(get_notif_service),
) -> ResultConfirmationService:
    smart_diag = SmartDiagnosisService(
        audit_logger=audit_logger,
        notif_service=notif_service,
    )
    return ResultConfirmationService(
        db=db,
        audit_logger=audit_logger,
        smart_diagnosis_service=smart_diag,
        notif_service=notif_service,
    )


async def get_override_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> ManualOverrideService:
    return ManualOverrideService(db=db, audit_logger=audit_logger)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{id}/confirm", response_model=ConfirmResultResponse, status_code=200)
async def confirm_result(
    id: uuid.UUID,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    service: ResultConfirmationService = Depends(get_confirmation_service),
) -> ConfirmResultResponse:
    """Confirm an analysis result. Triggers Smart Diagnosis automatically. Requires MEDTECH role."""
    confirmation = await service.confirm_result(
        result_id=id,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return ConfirmResultResponse.model_validate(confirmation)


@router.post("/{id}/override", response_model=OverrideResponse, status_code=200)
async def override_parameter(
    id: uuid.UUID,
    body: OverrideRequest,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH, UserRole.SUPERVISOR])),
    service: ManualOverrideService = Depends(get_override_service),
) -> OverrideResponse:
    """Override a single AI-generated parameter value.

    `body.original_ai_value` is accepted for API-contract compatibility but
    ignored — the service re-derives the original value from the stored
    `ai_findings` (source of truth), never trusting a client-supplied value.
    """
    override = await service.override_parameter(
        result_id=id,
        parameter=body.parameter,
        corrected_value=body.corrected_value,
        rationale=body.rationale,
        original_ai_value=body.original_ai_value,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return OverrideResponse.model_validate(override)
