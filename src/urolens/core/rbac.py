"""The canonical, session-revocation-aware auth/RBAC dependencies. Every
protected route should depend on `RequireRole` (which itself depends on
`get_current_user`), never re-implement token decoding or role checks
inline.
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import audit_logger
from .auth_service import decode_jwt, is_session_active

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
    """FastAPI dependency factory enforcing both authentication and role
    membership. Use as `Depends(RequireRole([UserRole.MEDTECH, ...]))` on any
    route that needs a role check — this is the canonical way to do it;
    ownership/role checks belong here, not solely inside a service.

    Args:
        allowed_roles: roles permitted to call the route. Compared
            case-insensitively against the caller's `role` claim.
    """

    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    async def __call__(self, request: Request, current_user: dict = Depends(get_current_user)) -> dict:
        """Reject the request with 403 if the authenticated caller's role
        isn't in `allowed_roles`; otherwise pass through `get_current_user`'s
        claims.

        Returns:
            The decoded JWT claims dict, unchanged from `get_current_user`.

        Raises:
            HTTPException: 403, if the caller's role isn't allowed. (401 is
                raised upstream by `get_current_user` if unauthenticated.)
        """
        user_role = current_user.get("role", "").lower()
        if user_role not in {r.lower() for r in self.allowed_roles}:
            raise _FORBIDDEN
        return current_user
