from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.middleware.rbac import get_current_user
from app.schemas.auth import LoginRequest, LoginResponse, LogoutResponse
from app.services import audit_logger
from app.services.auth_service import (
    close_session,
    create_session,
    get_user_by_username,
    increment_failed_attempts,
    issue_jwt,
    reset_failed_attempts,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
async def login(
    body: LoginRequest,
    request: Request,
):
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent")

    user = await get_user_by_username(body.username)
    if user is None:
        await audit_logger.log_login_failed(ip_address)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    if not verify_password(body.password, user["hashed_password"]):
        await increment_failed_attempts(user["user_id"])
        await audit_logger.log_login_failed(ip_address, user_id=user["user_id"])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    if user.get("locked_at") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account_locked",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account_inactive",
        )

    await reset_failed_attempts(user["user_id"])
    session_record = await create_session(
        user["user_id"], user["role"], ip_address, user_agent
    )
    token = issue_jwt(
        user["user_id"], user["role"], session_record["session_id"]
    )
    await audit_logger.log_login_success(
        user["user_id"], session_record["session_id"], ip_address
    )

    return LoginResponse(access_token=token, role=user["role"])


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
)
async def logout(
    request: Request,
    claims: dict = Depends(get_current_user),
):
    ip_address = request.client.host if request.client else "unknown"
    session_id = claims["session_id"]
    user_id = claims["user_id"]

    await close_session(session_id)
    await audit_logger.log_logout(user_id, session_id, ip_address)

    return LogoutResponse(message="Logged out successfully.")
