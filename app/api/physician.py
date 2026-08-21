from fastapi import APIRouter, Depends, Query, Request

from app.schemas.physician import (
    LabRequestCreateRequest,
    LabRequestCreateResponse,
    PhysicianPatientItem,
    PhysicianResultDetail,
    PhysicianResultListResponse,
)
from app.services import physician_service, physician_result_service
from src.urolens.core.rbac import RequireRole

router = APIRouter(prefix="/api/v1/physician", tags=["physician"])

_physician = RequireRole(["PHYSICIAN"])


@router.get("/patients/search", response_model=list[PhysicianPatientItem])
async def search_patients(
    q: str = Query(default="", min_length=1),
    claims: dict = Depends(_physician),
):
    return await physician_service.search_patients(q)


@router.post("/lab-requests", response_model=LabRequestCreateResponse, status_code=201)
async def create_lab_request(
    body: LabRequestCreateRequest,
    request: Request,
    claims: dict = Depends(_physician),
):
    return await physician_service.create_lab_request(
        data=body,
        physician_id=claims["user_id"],
        physician_username=claims["username"],
        request=request,
    )


@router.get("/results", response_model=PhysicianResultListResponse)
async def list_my_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    claims: dict = Depends(_physician),
):
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
    return await physician_result_service.get_result_detail(
        result_id=result_id,
        physician_id=claims["user_id"],
        request=request,
    )
