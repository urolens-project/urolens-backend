from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.middleware.rbac import get_current_user
from app.schemas.auth import LoginRequest, LoginResponse
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

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=message)
    exc.error_code = code  # type: ignore[attr-defined]
    return exc


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
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive.",
        )

    if user.get("locked_at") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked.",
        )

    if not verify_password(body.password, user["hashed_password"]):
        await increment_failed_attempts(user["user_id"])
        await audit_logger.log_login_failed(ip_address, user_id=user["user_id"])
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    if user.get("locked_at") is not None:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Your account is locked. Contact an administrator.",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is inactive. Contact an administrator.",
        )

    await reset_failed_attempts(user["user_id"])
    session_record = await create_session(
        user["user_id"], user["role"], ip_address, user_agent
    )
    token = issue_jwt(
        user["user_id"], user["username"], user["role"], session_record["session_id"]
    )
    await audit_logger.log_login_success(
        user["user_id"], session_record["session_id"], ip_address
    )

    return LoginResponse(
        access_token=token,
        role=user["role"],
        user_id=str(user["user_id"]),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    claims: dict = Depends(get_current_user),
):
    ip_address = request.client.host if request.client else "unknown"
    session_id = claims["session_id"]
    user_id = claims["user_id"]

    await close_session(session_id)
    await audit_logger.log_logout(user_id, session_id, ip_address)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
