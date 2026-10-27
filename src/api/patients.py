"""Patient intake routes (receptionist-facing): creation and search."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger, getAuditLogger
from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.patient import PatientCreateRequest, PatientResponse, PatientSearchItem
from src.services.patient_service import PatientService

router = APIRouter(tags=["Patients"])


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


@router.get("/api/v1/patients", response_model=list[PatientSearchItem])
async def searchPatients(
    q: str = Query(default="", min_length=3),
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: PatientService = Depends(getPatientService),
):
    """Search patients by name; see `PatientService.search_patients`.

    Returns a slim `patientId`/`patientUid`-only shape — full decrypted
    records are never exposed for a broad name-substring search. Distinct
    from `POST /api/v1/patients`'s `PatientResponse`, which stays full
    (create response, not search).
    """
    return await _service.searchPatients(q)
