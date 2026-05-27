import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.exceptions import NotFoundException
from ..middleware.rbac import RequireRole
from ..models.analysis_result import AnalysisResult
from ..models.user import UserRole
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
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    service: ManualOverrideService = Depends(get_override_service),
) -> OverrideResponse:
    """Override a single AI-generated parameter value. Requires MEDTECH role."""
    override = await service.override_parameter(
        result_id=id,
        parameter=body.parameter,
        corrected_value=body.corrected_value,
        rationale=body.rationale,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return OverrideResponse.model_validate(override)


@router.get("/{id}", response_model=ResultDetailResponse, status_code=200)
async def get_result(
    id: uuid.UUID,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    db: AsyncSession = Depends(get_db),
) -> ResultDetailResponse:
    """Returns the full analysis result including ai_findings and smart_diagnosis. Requires MEDTECH role."""
    stmt = select(AnalysisResult).options(selectinload(AnalysisResult.smart_diagnosis_output)).where(AnalysisResult.result_id == id)
    row = await db.execute(stmt)
    result = row.scalar_one_or_none()
    if result is None:
        raise NotFoundException(
            code="RESULT_NOT_FOUND",
            message=f"No result found with id {id}.",
        )

    smart_diag_data: Optional[dict] = None
    if result.smart_diagnosis_output:
        sd = result.smart_diagnosis_output
        smart_diag_data = {
            "gout_score":                sd.gout_score,
            "gn_score":                  sd.gn_score,
            "nephro_score":              sd.nephro_score,
            "no_significant_indicators": sd.no_significant_indicators,
            "evidence_map":              sd.evidence_map,
            "engine_version":            sd.engine_version,
        }

    return ResultDetailResponse(
        id=result.id,
        specimen_id=result.specimen_id,
        status=result.status,
        ai_findings=result.ai_findings or {},
        smart_diagnosis=smart_diag_data,
        smart_diagnosis_unavailable=result.smart_diagnosis_unavailable,
        confirmed_at=result.confirmed_at,
        confirmed_by=result.confirmed_by,
    )


@router.get("/{id}/smart-diagnosis", response_model=SmartDiagnosisResponse, status_code=200)
async def get_smart_diagnosis(
    id: uuid.UUID,
    current_user: dict = Depends(RequireRole([UserRole.SUPERVISOR])),
    db: AsyncSession = Depends(get_db),
) -> SmartDiagnosisResponse:
    """
    Returns the Smart Diagnosis output for a confirmed result.
    Requires SUPERVISOR role.
    """
    stmt = select(AnalysisResult).options(selectinload(AnalysisResult.smart_diagnosis_output)).where(AnalysisResult.result_id == id)
    row = await db.execute(stmt)
    result = row.scalar_one_or_none()
    if result is None:
        raise NotFoundException(
            code="RESULT_NOT_FOUND",
            message=f"No result found with id {id}.",
        )

    if result.smart_diagnosis_output is None:
        raise NotFoundException(
            code="SMART_DIAGNOSIS_NOT_FOUND",
            message=(
                "Smart Diagnosis output is not yet available for this result. "
                "It is generated automatically after MedTech confirmation."
            ),
        )

    sd = result.smart_diagnosis_output
    return SmartDiagnosisResponse(
        result_id=result.result_id,
        gout_score=sd.gout_score,
        gn_score=sd.gn_score,
        nephro_score=sd.nephro_score,
        no_significant_indicators=sd.no_significant_indicators,
        evidence_map=sd.evidence_map or {},
        engine_version=sd.engine_version,
        status=sd.status,
        generated_at=sd.generated_at,
    )
