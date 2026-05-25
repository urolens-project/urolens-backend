from __future__ import annotations

from typing import List

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..core.config import JWT_ALGORITHM, JWT_SIGNING_KEY
from ..models.user import User, UserRole

_security = HTTPBearer()

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Authentication required.",
)

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Insufficient permissions.",
)


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SIGNING_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except jwt.InvalidTokenError:
        raise _UNAUTHORIZED


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
) -> dict:
    """
    Validates the Bearer JWT and returns the decoded claims dict.
    Claims include: user_id, role, session_id.
    """
    return _decode_jwt(credentials.credentials)


class RequireRole:
    """
    FastAPI dependency that enforces role-based access.

    Usage:
        @router.post("/some-route")
        async def handler(user = Depends(RequireRole([UserRole.MEDTECH]))):
            ...
    """

    def __init__(self, allowed_roles: List[UserRole | str]):
        self.allowed_roles = [
            r.value if isinstance(r, UserRole) else r for r in allowed_roles
        ]

    async def __call__(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(_security),
    ) -> dict:
        claims = _decode_jwt(credentials.credentials)
        role = claims.get("role", "")
        if role not in self.allowed_roles:
            raise _FORBIDDEN
        return claims
