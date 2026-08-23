"""Physician-portal routes: patient search, lab request creation, and result
listing/detail.
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.database import getDb
from src.urolens.core.rbac import RequireRole
from src.urolens.schemas.lab_request import LabRequestCreateResponse
from src.urolens.schemas.physician import (
    LabRequestCreateRequest,
    PhysicianPatientItem,
    PhysicianResultDetail,
    PhysicianResultListResponse,
)
from src.urolens.services import (
    lab_request_service,
    physician_result_service,
    physician_service,
)

router = APIRouter(prefix="/api/v1/physician", tags=["physician"])

_physician = RequireRole(["PHYSICIAN"])


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
):
    """List the authenticated physician's results; see
    `physician_result_service.list_results`.
    """
    return await physician_result_service.listResults(
        physicianId=claims["user_id"],
        page=page,
        pageSize=pageSize,
    )


@router.get("/results/{result_id}", response_model=PhysicianResultDetail)
async def getResultDetail(
    result_id: str,
    request: Request,
    claims: dict = Depends(_physician),
):
    """Fetch one result's detail for the authenticated physician; see
    `physician_result_service.get_result_detail`.
    """
    return await physician_result_service.getResultDetail(
        resultId=result_id,
        physicianId=claims["user_id"],
        request=request,
    )
