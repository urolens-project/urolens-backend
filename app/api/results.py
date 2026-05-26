from fastapi import APIRouter, Depends

from app.middleware.rbac import RequireRole, get_current_user
from app.schemas.results import (
    ConfirmResultRequest,
    ConfirmResultResponse,
    OverrideParameterRequest,
    OverrideParameterResponse,
    SmartDiagnosisResponse,
)
from app.services import result_service

router = APIRouter(prefix="/api/v1/results", tags=["results"])


@router.get(
    "/{result_id}/smart-diagnosis",
    response_model=SmartDiagnosisResponse,
    summary="Get Smart Diagnosis output for a result",
)
async def get_smart_diagnosis(
    result_id: str,
    claims: dict = Depends(RequireRole(["SUPERVISOR"])),
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
