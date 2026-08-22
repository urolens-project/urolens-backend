"""Physician-portal routes: patient search, lab request creation, and result
listing/detail."""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.database import get_db
from src.urolens.core.rbac import RequireRole
from src.urolens.schemas.lab_request import LabRequestCreateResponse
from src.urolens.schemas.physician import (
    LabRequestCreateRequest,
    PhysicianPatientItem,
    PhysicianResultDetail,
    PhysicianResultListResponse,
)
from src.urolens.services import lab_request_service, physician_service, physician_result_service

router = APIRouter(prefix="/api/v1/physician", tags=["physician"])

_physician = RequireRole(["PHYSICIAN"])


@router.get("/patients/search", response_model=list[PhysicianPatientItem])
async def search_patients(
    q: str = Query(default="", min_length=1),
    claims: dict = Depends(_physician),
):
    """Search patients by name; see `physician_service.search_patients`."""
    return await physician_service.search_patients(q)


@router.post("/lab-requests", response_model=LabRequestCreateResponse, status_code=201)
async def create_lab_request(
    body: LabRequestCreateRequest,
    request: Request,
    claims: dict = Depends(_physician),
    db: AsyncSession = Depends(get_db),
):
    """Create a lab request on behalf of the authenticated physician; see
    `lab_request_service.create_lab_request`."""
    physician_id = uuid.UUID(claims["user_id"])
    ip_address = request.client.host if request.client else None
    return await lab_request_service.create_lab_request(
        db,
        encoded_by=physician_id,
        patient_id=body.patient_id,
        test_type=body.test_type,
        clinical_notes=body.clinical_notes,
        physician_id=physician_id,
        physician_name=claims["username"],
        notify_receptionists=True,
        ip_address=ip_address,
    )


@router.get("/results", response_model=PhysicianResultListResponse)
async def list_my_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    claims: dict = Depends(_physician),
):
    """List the authenticated physician's results; see
    `physician_result_service.list_results`."""
    return await physician_result_service.list_results(
        physician_id=claims["user_id"],
        page=page,
        page_size=page_size,
    )


@router.get("/results/{result_id}", response_model=PhysicianResultDetail)
async def get_result_detail(
    result_id: str,
    request: Request,
    claims: dict = Depends(_physician),
):
    """Fetch one result's detail for the authenticated physician; see
    `physician_result_service.get_result_detail`."""
    return await physician_result_service.get_result_detail(
        result_id=result_id,
        physician_id=claims["user_id"],
        request=request,
    )
