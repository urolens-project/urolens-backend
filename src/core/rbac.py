"""The canonical, session-revocation-aware auth/RBAC dependencies. Every
protected route should depend on `RequireRole` (which itself depends on
`get_current_user`), never re-implement token decoding or role checks
inline.
"""
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import audit_logger
from .auth_service import decodeJwt, isSessionActive

securityScheme = HTTPBearer()
logger = logging.getLogger(__name__)

def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )

def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Insufficient permissions.",
    )

async def getCurrentUser(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(securityScheme),
) -> dict:
    """FastAPI dependency resolving the caller's identity from their Bearer
    JWT, rejecting it if the token is invalid or its session has been revoked.

    Every rejection is recorded via `audit_logger.log_access_denied` before
    raising.

    Returns:
        The decoded JWT claims dict (`user_id`, `username`, `role`,
        `session_id`, ...).

    Raises:
        HTTPException: 401, if the token fails to decode/verify, or if its
            session is missing/inactive (revoked or logged out).
    """
    token = credentials.credentials
    ipAddress = request.client.host if request.client else "unknown"

    try:
        claims = decodeJwt(token)
    except Exception as err:
        logger.warning("JWT decode failed from %s", ipAddress, exc_info=True)
        await audit_logger.logAccessDenied(ipAddress)
        raise _unauthorized() from err

    sessionId = claims.get("session_id")
    if not sessionId or not await isSessionActive(sessionId):
        await audit_logger.logAccessDenied(ipAddress, userId=claims.get("user_id"))
        raise _unauthorized()

    return claims


class RequireRole:
    """FastAPI dependency factory enforcing both authentication and role
    membership. Use as `Depends(RequireRole([UserRole.MEDTECH, ...]))` on any
    route that needs a role check — this is the canonical way to do it;
    ownership/role checks belong here, not solely inside a service.

    Args:
        allowed_roles: roles permitted to call the route. Compared
            case-insensitively against the caller's `role` claim.
    """

    def __init__(self, allowedRoles: list[str]):
        self.allowedRoles = allowedRoles

    async def __call__(self, request: Request, currentUser: dict = Depends(getCurrentUser)) -> dict:
        """Reject the request with 403 if the authenticated caller's role
        isn't in `allowed_roles`; otherwise pass through `get_current_user`'s
        claims.

        Returns:
            The decoded JWT claims dict, unchanged from `get_current_user`.

        Raises:
            HTTPException: 403, if the caller's role isn't allowed. (401 is
                raised upstream by `get_current_user` if unauthenticated.)
        """
        userRole = currentUser.get("role", "").lower()
        if userRole not in {r.lower() for r in self.allowedRoles}:
            raise _forbidden()
        return currentUser
