"""Patient-portal result routes: listing, detail, and PDF download."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger, getAuditLogger
from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.patient_portal import (
    PatientResultDetailResponse,
    PatientResultItem,
)
from src.services.patient_result_service import PatientResultService
from src.services.patient_service import PatientService
from src.services.pdf_service import generateResultPdf

router = APIRouter()


async def getPatientResultService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> PatientResultService:
    """FastAPI dependency constructing a request-scoped `PatientResultService`."""
    return PatientResultService(db=db, auditLogger=auditLogger)


async def getPatientService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> PatientService:
    """FastAPI dependency constructing a request-scoped `PatientService`."""
    return PatientService(db=db, auditLogger=auditLogger)


@router.get("/api/v1/patient/results", response_model=list[PatientResultItem])
async def getMyResults(
    currentUser: dict = Depends(RequireRole([UserRole.PATIENT])),
    _service: PatientResultService = Depends(getPatientResultService),
):
    """List the authenticated patient's results; see
    `PatientResultService.get_patient_results`.
    """
    return await _service.getPatientResults(currentUser["user_id"])


@router.get("/api/v1/patient/results/{result_id}", response_model=PatientResultDetailResponse)
async def getResultDetail(
    result_id: UUID,
    request: Request,
    currentUser: dict = Depends(RequireRole([UserRole.PATIENT])),
    _service: PatientResultService = Depends(getPatientResultService),
):
    """Fetch one result's detail for the authenticated patient; see
    `PatientResultService.get_result_detail`.
    """
    return await _service.getResultDetail(result_id, currentUser["user_id"], request)


@router.get("/api/v1/patient/results/{result_id}/pdf")
async def downloadResultPdf(
    result_id: UUID,
    request: Request,
    currentUser: dict = Depends(RequireRole([UserRole.PATIENT])),
    _resultService: PatientResultService = Depends(getPatientResultService),
    _patientService: PatientService = Depends(getPatientService),
):
    """Render and return the authenticated patient's result as a downloadable PDF."""
    result = await _resultService.getResultDetail(
        result_id, currentUser["user_id"], request
    )
    patient = await _patientService.getPatientByUserId(currentUser["user_id"])
    patientName = f"{patient.firstName} {patient.lastName}"
    pdfBytes = generateResultPdf(result, patientName)
    return Response(
        content=pdfBytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=result_{result_id}.pdf"
        },
    )
