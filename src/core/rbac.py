"""The canonical, session-revocation-aware auth/RBAC dependencies. Every
protected route should depend on `RequireRole` (which itself depends on
`get_current_user`), never re-implement token decoding or role checks
inline.
"""
import logging

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import audit_logger
from .auth_service import decodeJwt, isSessionActive, sessionEndReason

securityScheme = HTTPBearer()
logger = logging.getLogger(__name__)

def _unauthorized(code: str | None = None, message: str = "Authentication required.") -> HTTPException:
    # Without a code the envelope says UNAUTHORIZED. The specific codes let the
    # client tell "log in again" cases apart (UROLENS-244).
    exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)
    if code:
        exc.errorCode = code  # type: ignore[attr-defined]
    return exc

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
    JWT, rejecting it if the token is invalid or its session has been
    revoked — including a session `is_session_active` finds idle past its
    role's inactivity timeout (UROLENS-167), which it revokes as part of
    that check.

    Every rejection is recorded via `audit_logger.log_access_denied` before
    raising.

    Returns:
        The decoded JWT claims dict (`user_id`, `username`, `role`,
        `session_id`, ...).

    Raises:
        HTTPException: 401 `SESSION_EXPIRED`, if the token has expired;
            `SESSION_IDLE`, if its session was ended for inactivity
            (UROLENS-167/245); `SESSION_ENDED`, if it's missing or was logged
            out or revoked; `UNAUTHORIZED`, if it fails to decode or verify.
    """
    token = credentials.credentials
    ipAddress = request.client.host if request.client else "unknown"

    try:
        claims = decodeJwt(token)
    except jwt.ExpiredSignatureError as err:
        await audit_logger.logAccessDenied(ipAddress)
        raise _unauthorized("SESSION_EXPIRED", "Your session has expired. Please log in again.") from err
    except Exception as err:
        logger.warning("JWT decode failed from %s", ipAddress, exc_info=True)
        await audit_logger.logAccessDenied(ipAddress)
        raise _unauthorized() from err

    sessionId = claims.get("session_id")
    if not sessionId or not await isSessionActive(sessionId, ipAddress=ipAddress):
        await audit_logger.logAccessDenied(ipAddress, userId=claims.get("user_id"))
        # Only a rejection pays for this second read (UROLENS-245).
        if sessionId and await sessionEndReason(sessionId) == "IDLE":
            raise _unauthorized("SESSION_IDLE", "You were signed out due to inactivity. Please log in again.")
        raise _unauthorized("SESSION_ENDED", "Your session has ended. Please log in again.")

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
