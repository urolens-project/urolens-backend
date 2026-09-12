"""Patient intake routes (receptionist-facing): creation and search."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger, getAuditLogger
from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.patient import PatientCreateRequest, PatientResponse
from src.services.patient_service import PatientService

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
    """Search patients by Patient ID only, not name — see
    `PatientService.searchPatients` for why.
    """
    return await _service.searchPatients(q)
