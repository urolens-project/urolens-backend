import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from supabase import AsyncClient

from app.db.supabase import get_supabase
from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.exceptions import NotFoundException
from ..middleware.rbac import RequireRole
from ..models.user import UserRole
from ..services.manual_override_service import ManualOverrideService
from ..services.notification_service import NotificationService
from ..services.result_confirmation_service import ResultConfirmationService
from ..services.smart_diagnosis_service import SmartDiagnosisService
from app.schemas.results import (
    AnnotationRequest,
    AnnotationResponse,
    ApproveRequest,
    ApproveResponse,
    EscalateRequest,
    EscalateResponse,
    FullResultDetail,
    PendingResultListResponse,
    ReturnRequest,
    ReturnResponse,
)
from app.services import result_review_service

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
    rationale: str = Field(..., min_length=1, max_length=2000)

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


class ResultDetailResponse(BaseModel):
    id: uuid.UUID
    specimen_id: uuid.UUID
    status: str
    ai_findings: dict[str, Any]
    smart_diagnosis: Optional[dict[str, Any]]
    smart_diagnosis_unavailable: bool
    confirmed_at: Optional[datetime]
    confirmed_by: Optional[uuid.UUID]

    model_config = {"from_attributes": True}


class SmartDiagnosisResponse(BaseModel):
    result_id: uuid.UUID
    gout_score: str
    gn_score: str
    nephro_score: str
    no_significant_indicators: bool
    evidence_map: dict[str, Any]
    engine_version: str
    status: str
    generated_at: datetime

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
    """Override a single AI-generated parameter value. Requires MEDTECH or SUPERVISOR role."""
    override = await service.override_parameter(
        result_id=id,
        parameter=body.parameter,
        corrected_value=body.corrected_value,
        rationale=body.rationale,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return OverrideResponse.model_validate(override)


# ── Supervisor routes (must be before /{id} to avoid path conflicts) ──────────

_supervisor = RequireRole([UserRole.SUPERVISOR])


@router.get("/pending", response_model=PendingResultListResponse)
async def list_pending_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.get_pending(page=page, page_size=page_size)


@router.get("/{result_id}/full", response_model=FullResultDetail)
async def get_full_result(
    result_id: str,
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.get_full_result(result_id=result_id)


@router.patch("/{result_id}/annotate", response_model=AnnotationResponse)
async def annotate_result(
    result_id: str,
    body: AnnotationRequest,
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.save_annotation(
        result_id=result_id,
        user_id=current_user["user_id"],
        annotation_notes=body.annotation_notes,
        spatial_annotations=body.spatial_annotations,
    )


@router.post("/{result_id}/approve", response_model=ApproveResponse)
async def approve_result(
    result_id: str,
    body: ApproveRequest,
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.approve_result(
        result_id=result_id,
        user_id=current_user["user_id"],
        notes=body.notes,
    )


@router.post("/{result_id}/return", response_model=ReturnResponse)
async def return_result(
    result_id: str,
    body: ReturnRequest,
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.return_result(
        result_id=result_id,
        user_id=current_user["user_id"],
        reason=body.reason,
    )


@router.post("/{result_id}/escalate", response_model=EscalateResponse)
async def escalate_result(
    result_id: str,
    body: EscalateRequest,
    current_user: dict = Depends(_supervisor),
):
    return await result_review_service.escalate_result(
        result_id=result_id,
        user_id=current_user["user_id"],
        escalation_path=body.escalation_path,
        escalation_note=body.escalation_note,
    )


# ── MedTech / Supervisor detail routes (Supabase) ─────────────────────────────

@router.get("/{id}", response_model=ResultDetailResponse, status_code=200)
async def get_result(
    id: uuid.UUID,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH, UserRole.SUPERVISOR])),
    db: AsyncClient = Depends(get_supabase),
) -> ResultDetailResponse:
    """Returns the analysis result including ai_findings and smart_diagnosis. Requires MEDTECH or SUPERVISOR role."""
    ar_res = await db.table("analysis_results").select(
        "result_id, specimen_id, status, ai_findings, smart_diagnosis_unavailable, confirmed_at, confirmed_by"
    ).eq("result_id", str(id)).execute()

    rows = ar_res.data or []
    if not rows:
        raise NotFoundException(code="RESULT_NOT_FOUND", message=f"No result found with id {id}.")
    ar = rows[0]

    sd_res = await db.table("smart_diagnosis_outputs").select(
        "gout_score, gn_score, nephro_score, no_significant_indicators, evidence_map, engine_version"
    ).eq("result_id", str(id)).limit(1).execute()
    sd_rows = sd_res.data or []
    smart_diag_data = sd_rows[0] if sd_rows else None

    return ResultDetailResponse(
        id=ar["result_id"],
        specimen_id=ar["specimen_id"],
        status=ar["status"],
        ai_findings=ar.get("ai_findings") or {},
        smart_diagnosis=smart_diag_data,
        smart_diagnosis_unavailable=ar.get("smart_diagnosis_unavailable", False),
        confirmed_at=ar.get("confirmed_at"),
        confirmed_by=ar.get("confirmed_by"),
    )


@router.get("/{id}/smart-diagnosis", response_model=SmartDiagnosisResponse, status_code=200)
async def get_smart_diagnosis(
    id: uuid.UUID,
    current_user: dict = Depends(RequireRole([UserRole.SUPERVISOR])),
    db: AsyncClient = Depends(get_supabase),
) -> SmartDiagnosisResponse:
    """
    Returns the Smart Diagnosis output for a confirmed result.
    Requires SUPERVISOR role.
    """
    sd_res = await db.table("smart_diagnosis_outputs").select(
        "result_id, gout_score, gn_score, nephro_score, no_significant_indicators, "
        "evidence_map, engine_version, status, generated_at"
    ).eq("result_id", str(id)).limit(1).execute()

    sd_rows = sd_res.data or []
    if not sd_rows:
        ar_res = await db.table("analysis_results").select("result_id").eq("result_id", str(id)).limit(1).execute()
        if not (ar_res.data or []):
            raise NotFoundException(code="RESULT_NOT_FOUND", message=f"No result found with id {id}.")
        raise NotFoundException(
            code="SMART_DIAGNOSIS_NOT_FOUND",
            message="Smart Diagnosis output is not yet available. It is generated after MedTech confirmation.",
        )

    sd = sd_rows[0]
    return SmartDiagnosisResponse(
        result_id=sd["result_id"],
        gout_score=sd["gout_score"],
        gn_score=sd["gn_score"],
        nephro_score=sd["nephro_score"],
        no_significant_indicators=sd["no_significant_indicators"],
        evidence_map=sd.get("evidence_map") or {},
        engine_version=sd["engine_version"],
        status=sd["status"],
        generated_at=sd["generated_at"],
    )
