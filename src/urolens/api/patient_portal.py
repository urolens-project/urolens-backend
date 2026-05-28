import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from app.config import SUPABASE_PDF_BUCKET
from app.db.supabase import supabase
from app.middleware.rbac import RequireRole
from src.urolens.core.audit_logger import AuditLogger, get_audit_logger
from src.urolens.core.enums import UserRole
from src.urolens.schemas.patient_portal import PatientResultDetailResponse, PatientResultItem
from src.urolens.services.patient_result_service import PatientResultService
from src.urolens.services.patient_service import PatientService
from src.urolens.services.pdf_service import generate_result_pdf
from supabase import AsyncClient
from app.db.supabase import get_supabase

log = logging.getLogger(__name__)

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


@router.get("/api/v1/patient/results", response_model=list[PatientResultItem])
async def get_my_results(
    current_user: dict = Depends(RequireRole([UserRole.PATIENT])),
    service: PatientResultService = Depends(get_patient_result_service),
):
    return await service.get_patient_results(current_user["user_id"])


@router.get("/api/v1/patient/results/{result_id}", response_model=PatientResultDetailResponse)
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

    pdf_bytes = generate_result_pdf(result, patient_name, str(result_id))

    # Upload to Supabase Storage and return a short-lived signed URL
    storage_key = f"{result_id}.pdf"
    try:
        await supabase.storage.from_(SUPABASE_PDF_BUCKET).upload(
            path=storage_key,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
        signed = await supabase.storage.from_(SUPABASE_PDF_BUCKET).create_signed_url(
            path=storage_key,
            expires_in=300,  # 5 minutes
        )
        # supabase-py may return a Pydantic model or a dict depending on version
        if hasattr(signed, "signed_url"):
            url = signed.signed_url
        elif isinstance(signed, dict):
            url = signed.get("signedURL") or signed.get("signed_url")
        else:
            url = None

        if url:
            return {"url": url}
    except Exception as exc:
        log.warning("PDF Storage upload/sign failed for result %s: %s", result_id, exc)

    # Fallback: stream bytes directly if storage is unavailable
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=result_{result_id}.pdf"},
    )
