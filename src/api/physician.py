"""Physician-portal routes: patient search, lab request creation, and result
listing/detail.
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger, getAuditLogger
from src.core.database import getDb
from src.core.rbac import RequireRole
from src.schemas.lab_request import LabRequestCreateResponse
from src.schemas.physician import (
    LabRequestCreateRequest,
    PhysicianPatientItem,
    PhysicianResultDetail,
    PhysicianResultListResponse,
)
from src.services import (
    lab_request_service,
    physician_service,
)
from src.services.physician_result_service import PhysicianResultService

router = APIRouter(prefix="/api/v1/physician", tags=["physician"])

_physician = RequireRole(["PHYSICIAN"])


async def getPhysicianResultService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> PhysicianResultService:
    """FastAPI dependency constructing a request-scoped `PhysicianResultService`."""
    return PhysicianResultService(db=db, auditLogger=auditLogger)


@router.get("/patients/search", response_model=list[PhysicianPatientItem])
async def searchPatients(
    q: str = Query(default="", min_length=1),
    claims: dict = Depends(_physician),
):
    """Search patients by name; see `physician_service.search_patients`."""
    return await physician_service.searchPatients(q)


@router.post("/lab-requests", response_model=LabRequestCreateResponse, status_code=201)
async def createLabRequest(
    body: LabRequestCreateRequest,
    request: Request,
    claims: dict = Depends(_physician),
    db: AsyncSession = Depends(getDb),
):
    """Create a lab request on behalf of the authenticated physician; see
    `lab_request_service.create_lab_request`.
    """
    physicianId = uuid.UUID(claims["user_id"])
    ipAddress = request.client.host if request.client else None
    return await lab_request_service.createLabRequest(
        db,
        encodedBy=physicianId,
        patientId=body.patientId,
        testType=body.testType,
        clinicalNotes=body.clinicalNotes,
        physicianId=physicianId,
        physicianName=claims["username"],
        notifyReceptionists=True,
        ipAddress=ipAddress,
    )


@router.get("/results", response_model=PhysicianResultListResponse)
async def listMyResults(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    claims: dict = Depends(_physician),
    _service: PhysicianResultService = Depends(getPhysicianResultService),
):
    """List the authenticated physician's results; see
    `PhysicianResultService.list_results`.
    """
    return await _service.listResults(
        physicianId=uuid.UUID(claims["user_id"]),
        page=page,
        pageSize=pageSize,
    )


@router.get("/results/{result_id}", response_model=PhysicianResultDetail)
async def getResultDetail(
    result_id: uuid.UUID,
    request: Request,
    claims: dict = Depends(_physician),
    _service: PhysicianResultService = Depends(getPhysicianResultService),
):
    """Fetch one result's detail for the authenticated physician; see
    `PhysicianResultService.get_result_detail`.
    """
    return await _service.getResultDetail(
        resultId=result_id,
        physicianId=uuid.UUID(claims["user_id"]),
        request=request,
    )
