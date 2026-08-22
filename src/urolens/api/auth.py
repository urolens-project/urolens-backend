"""Staff login/logout routes."""
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status

from src.urolens.core import audit_logger
from src.urolens.core.rbac import get_current_user
from src.urolens.core.auth_service import (
    close_session,
    create_session,
    get_user_by_username,
    increment_failed_attempts,
    issue_jwt,
    reset_failed_attempts,
    verify_password,
)
from src.urolens.schemas.auth import LoginRequest, LoginResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    # Builds an HTTPException carrying a machine-readable error_code attribute.
    exc = HTTPException(status_code=status_code, detail=message)
    exc.error_code = code  # type: ignore[attr-defined]
    return exc


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(body: LoginRequest, request: Request, background_tasks: BackgroundTasks):
    """Authenticate a staff login and issue an access token.

    Checks run in a fixed order — user exists, password correct, account not
    locked, account active — with the password check deliberately performed
    before the lock/active checks (so a lock only triggers on a correct
    username, and to avoid revealing account state via timing). The
    `LOGIN_SUCCESS` audit write is deferred to a background task so it
    doesn't block the response.

    Raises:
        HTTPException: 401 (`INVALID_CREDENTIALS`), for an unknown username
            or wrong password. 423 (`ACCOUNT_LOCKED`), if the account is
            locked. 403 (`ACCOUNT_INACTIVE`), if the account is inactive.
    """
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent")

    # 1. User must exist
    user = await get_user_by_username(body.username)
    if user is None:
        await audit_logger.log_login_failed(ip_address)
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    # 2. Password must be correct (check before lock/active to avoid timing attacks)
    if not await verify_password(body.password, user["hashed_password"]):
        await asyncio.gather(
            increment_failed_attempts(user["user_id"]),
            audit_logger.log_login_failed(ip_address, user_id=user["user_id"]),
        )
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    # 3. Account must not be locked (checked after password so lock only triggers on correct username)
    if user.get("locked_at") is not None:
        raise _api_error(status.HTTP_423_LOCKED, "ACCOUNT_LOCKED", "Your account is locked. Contact an administrator.")

    # 4. Account must be active
    if not user.get("is_active", True):
        raise _api_error(status.HTTP_403_FORBIDDEN, "ACCOUNT_INACTIVE", "Your account is inactive. Contact an administrator.")

    # reset_failed_attempts and create_session are independent — run in parallel
    _, session_record = await asyncio.gather(
        reset_failed_attempts(user["user_id"]),
        create_session(user["user_id"], user["role"], ip_address, user_agent),
    )
    token = issue_jwt(user["user_id"], user["username"], user["role"], session_record["session_id"])

    # Audit write doesn't need to block the response
    background_tasks.add_task(
        audit_logger.log_login_success, user["user_id"], session_record["session_id"], ip_address
    )

    return LoginResponse(access_token=token, role=user["role"], user_id=str(user["user_id"]))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, claims: dict = Depends(get_current_user)):
    """Close the authenticated staff session and record a `LOGOUT` audit entry."""
    ip_address = request.client.host if request.client else "unknown"
    await close_session(claims["session_id"])
    await audit_logger.log_logout(claims["user_id"], claims["session_id"], ip_address)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
