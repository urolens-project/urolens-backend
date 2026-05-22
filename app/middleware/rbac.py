from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services import audit_logger
from app.services.auth_service import decode_jwt, is_session_active

security_scheme = HTTPBearer()


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )

    session_id = claims.get("session_id")
    if not session_id or not await is_session_active(session_id):
        await audit_logger.log_access_denied(
            ip_address, user_id=claims.get("user_id")
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )

    return claims
