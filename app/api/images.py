from fastapi import APIRouter, Depends

from app.middleware.rbac import get_current_user
from app.schemas.images import DiscardImageResponse
from app.services import image_service

router = APIRouter(prefix="/api/v1/images", tags=["images"])


@router.post("/{image_id}/discard", response_model=DiscardImageResponse)
async def discard_image(
    image_id: str,
    claims: dict = Depends(get_current_user),
):
    return await image_service.discard_image(
        image_id=image_id,
        user_id=claims["user_id"],
    )
