from fastapi import APIRouter, Depends, Query, Request
from supabase import AsyncClient

from app.db.supabase import get_supabase
from app.middleware.rbac import RequireRole
from src.urolens.core.audit_logger import AuditLogger, get_audit_logger
from src.urolens.core.enums import UserRole
from src.urolens.schemas.patient import PatientCreateRequest, PatientResponse
from src.urolens.services.patient_service import PatientService

router = APIRouter()


async def get_patient_service(
    db: AsyncClient = Depends(get_supabase),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> PatientService:
    return PatientService(db=db, audit_logger=audit_logger)


@router.post("/api/v1/patients", response_model=PatientResponse, status_code=201)
async def create_patient(
    data: PatientCreateRequest,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: PatientService = Depends(get_patient_service),
):
    return await service.create_patient(data, current_user["user_id"], request)


@router.get("/api/v1/patients", response_model=list[PatientResponse])
async def search_patients(
    q: str = Query(default="", min_length=1),
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: PatientService = Depends(get_patient_service),
):
    return await service.search_patients(q)
