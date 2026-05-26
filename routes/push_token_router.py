# urolens-backend/routes/push_token_router.py

"""
Route: POST /api/v1/users/me/push-token

Stores the Expo push token for the authenticated MedTech so the backend
can target their device when events occur (EPIC-MOB-08).

Single Responsibility: this router ONLY handles push token persistence.
Notification dispatch lives in notification_service.py.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models.user import User
from routes.dependencies import get_current_medtech_user
from schemas.push_token_schema import PushTokenRegisterRequest
from services.audit_logger import AuditLogger

router = APIRouter(prefix="/api/v1/users/me", tags=["push-tokens"])


@router.post(
    "/push-token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register Expo push token for the authenticated MedTech",
)
async def register_push_token(
    body: PushTokenRegisterRequest,
    current_user: User = Depends(get_current_medtech_user),
    session: AsyncSession = Depends(get_session),
    audit: AuditLogger = Depends(AuditLogger),
) -> None:
    """
    Called by the mobile app on every login / token refresh.
    Idempotent — safe to call multiple times with the same token.
    """
    current_user.expo_push_token = body.expo_push_token
    session.add(current_user)

    await audit.record(
        "PUSH_TOKEN_REGISTERED",
        user_id=current_user.id,
        metadata={"token_prefix": body.expo_push_token[:20]},
    )

    await session.commit()