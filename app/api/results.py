from fastapi import APIRouter, Depends, Query

from app.middleware.rbac import RequireRole, get_current_user
from app.schemas.results import (
    AnnotationRequest,
    AnnotationResponse,
    ApproveRequest,
    ApproveResponse,
    ConfirmResultRequest,
    ConfirmResultResponse,
    EscalateRequest,
    EscalateResponse,
    FullResultDetail,
    OverrideParameterRequest,
    OverrideParameterResponse,
    PendingResultListResponse,
    ReturnRequest,
    ReturnResponse,
    SmartDiagnosisResponse,
)
from app.services import result_service, result_review_service

router = APIRouter(prefix="/api/v1/results", tags=["results"])

_supervisor = RequireRole(["SUPERVISOR"])


# ── MedTech endpoints ─────────────────────────────────────────────────────────

@router.get(
    "/{result_id}/smart-diagnosis",
    response_model=SmartDiagnosisResponse,
    summary="Get Smart Diagnosis output for a result",
)
async def get_smart_diagnosis(
    result_id: str,
    claims: dict = Depends(_supervisor),
):
    return await result_service.get_smart_diagnosis(result_id=result_id)


@router.post("/{result_id}/confirm", response_model=ConfirmResultResponse)
async def confirm_result(
    result_id: str,
    body: ConfirmResultRequest,
    claims: dict = Depends(get_current_user),
):
    return await result_service.confirm_result(
        result_id=result_id,
        user_id=claims["user_id"],
        notes=body.notes,
    )


@router.post("/{result_id}/override", response_model=OverrideParameterResponse)
async def override_parameter(
    result_id: str,
    body: OverrideParameterRequest,
    claims: dict = Depends(get_current_user),
):
    return await result_service.override_parameter(
        result_id=result_id,
        user_id=claims["user_id"],
        parameter_name=body.parameter_name,
        original_ai_value=body.original_ai_value,
        corrected_value=body.corrected_value,
        rationale=body.rationale,
    )


# ── Supervisor endpoints ───────────────────────────────────────────────────────

@router.get("/pending", response_model=PendingResultListResponse)
async def list_pending_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.get_pending(page=page, page_size=page_size)


@router.get("/{result_id}", response_model=FullResultDetail)
async def get_full_result(
    result_id: str,
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.get_full_result(result_id=result_id)


@router.patch("/{result_id}/annotate", response_model=AnnotationResponse)
async def annotate_result(
    result_id: str,
    body: AnnotationRequest,
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.save_annotation(
        result_id=result_id,
        user_id=claims["user_id"],
        annotation_notes=body.annotation_notes,
    )


@router.post("/{result_id}/approve", response_model=ApproveResponse)
async def approve_result(
    result_id: str,
    body: ApproveRequest,
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.approve_result(
        result_id=result_id,
        user_id=claims["user_id"],
        notes=body.notes,
    )


@router.post("/{result_id}/return", response_model=ReturnResponse)
async def return_result(
    result_id: str,
    body: ReturnRequest,
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.return_result(
        result_id=result_id,
        user_id=claims["user_id"],
        reason=body.reason,
    )


@router.post("/{result_id}/escalate", response_model=EscalateResponse)
async def escalate_result(
    result_id: str,
    body: EscalateRequest,
    claims: dict = Depends(_supervisor),
):
    return await result_review_service.escalate_result(
        result_id=result_id,
        user_id=claims["user_id"],
        escalation_path=body.escalation_path,
        escalation_note=body.escalation_note,
    )
