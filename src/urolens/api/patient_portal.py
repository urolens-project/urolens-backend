from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from supabase import AsyncClient

from app.db.supabase import get_supabase
from app.middleware.rbac import RequireRole
from src.urolens.core.audit_logger import AuditLogger, get_audit_logger
from src.urolens.core.enums import UserRole
from src.urolens.schemas.patient_portal import PatientResultDetail, PatientResultSummary
from src.urolens.services.patient_result_service import PatientResultService
from src.urolens.services.patient_service import PatientService
from src.urolens.services.pdf_service import generate_result_pdf

router = APIRouter()


async def get_patient_result_service(
    db: AsyncClient = Depends(get_supabase),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> PatientResultService:
    return PatientResultService(db=db, audit_logger=audit_logger)


async def get_patient_service(
    db: AsyncClient = Depends(get_supabase),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> PatientService:
    return PatientService(db=db, audit_logger=audit_logger)


@router.get("/api/v1/patient/results", response_model=list[PatientResultSummary])
async def get_my_results(
    current_user: dict = Depends(RequireRole([UserRole.PATIENT])),
    service: PatientResultService = Depends(get_patient_result_service),
):
    return await service.get_patient_results(current_user["user_id"])


@router.get("/api/v1/patient/results/{result_id}", response_model=PatientResultDetail)
async def get_result_detail(
    result_id: UUID,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.PATIENT])),
    service: PatientResultService = Depends(get_patient_result_service),
):
    return await service.get_result_detail(result_id, current_user["user_id"], request)


@router.get("/api/v1/patient/results/{result_id}/pdf")
async def download_result_pdf(
    result_id: UUID,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.PATIENT])),
    result_service: PatientResultService = Depends(get_patient_result_service),
    patient_service: PatientService = Depends(get_patient_service),
):
    result = await result_service.get_result_detail(
        result_id, current_user["user_id"], request
    )
    patient = await patient_service.get_patient_by_user_id(current_user["user_id"])
    patient_name = f"{patient.first_name} {patient.last_name}"
    pdf_bytes = generate_result_pdf(result, patient_name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=result_{result_id}.pdf"
        },
    )
