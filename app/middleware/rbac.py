from typing import List

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services import audit_logger
from app.services.auth_service import decode_jwt, is_session_active

security_scheme = HTTPBearer()

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Authentication required.",
)

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Insufficient permissions.",
)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
) -> dict:
    token = credentials.credentials
    ip_address = request.client.host if request.client else "unknown"

    try:
        claims = decode_jwt(token)
    except Exception:
        await audit_logger.log_access_denied(ip_address)
        raise _UNAUTHORIZED

    session_id = claims.get("session_id")
    if not session_id or not await is_session_active(session_id):
        await audit_logger.log_access_denied(ip_address, user_id=claims.get("user_id"))
        raise _UNAUTHORIZED

    return claims


class RequireRole:
    def __init__(self, allowed_roles: List[str]):
        self.allowed_roles = allowed_roles

    async def __call__(self, request: Request, current_user: dict = Depends(get_current_user)) -> dict:
        user_role = current_user.get("role", "").lower()
        if user_role not in {r.lower() for r in self.allowed_roles}:
            raise _FORBIDDEN
        return current_user
