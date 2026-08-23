"""Patient intake routes (receptionist-facing): creation and search."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.audit_logger import AuditLogger, getAuditLogger
from src.urolens.core.database import getDb
from src.urolens.core.enums import UserRole
from src.urolens.core.rbac import RequireRole
from src.urolens.schemas.patient import PatientCreateRequest, PatientResponse
from src.urolens.services.patient_service import PatientService

router = APIRouter()


async def getPatientService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> PatientService:
    """FastAPI dependency constructing a request-scoped `PatientService`."""
    return PatientService(db=db, auditLogger=auditLogger)


@router.post("/api/v1/patients", response_model=PatientResponse, status_code=201)
async def createPatient(
    data: PatientCreateRequest,
    request: Request,
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: PatientService = Depends(getPatientService),
):
    """Create a patient record with a linked portal account; see
    `PatientService.create_patient`.
    """
    return await _service.createPatient(data, currentUser["user_id"], request)


@router.get("/api/v1/patients", response_model=list[PatientResponse])
async def searchPatients(
    q: str = Query(default="", min_length=1),
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: PatientService = Depends(getPatientService),
):
    """Search patients by name; see `PatientService.search_patients`."""
    return await _service.searchPatients(q)
