from fastapi import APIRouter, Depends

from app.middleware.rbac import get_current_user
from app.schemas.results import ConfirmResultRequest, ConfirmResultResponse
from app.services import result_service

router = APIRouter(prefix="/api/v1/results", tags=["results"])


@router.post("/{result_id}/confirm", response_model=ConfirmResultResponse)
async def confirm_result(
    result_id: str,
    body: ConfirmResultRequest,
    claims: dict = Depends(get_current_user),
):
    return await result_service.confirm_result(
        result_id=result_id,
        user_id=claims["user_id"],
        notes=body.notes,
    )
