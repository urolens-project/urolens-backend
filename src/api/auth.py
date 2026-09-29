"""Staff login/logout routes."""
import asyncio

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)

from src.core import audit_logger
from src.core.auth_service import (
    LOCKOUT_MINUTES,
    closeSession,
    createSession,
    getUserByUsername,
    incrementFailedAttempts,
    isLockedOut,
    issueJwt,
    resetFailedAttempts,
    spendPasswordCheck,
    verifyPassword,
)
from src.core.rate_limit import clearLoginRateLimit, enforceLoginRateLimit
from src.core.rbac import getCurrentUser
from src.schemas.auth import LoginRequest, LoginResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _apiError(statusCode: int, code: str, message: str) -> HTTPException:
    # Builds an HTTPException carrying a machine-readable error_code attribute.
    exc = HTTPException(status_code=statusCode, detail=message)
    exc.errorCode = code  # type: ignore[attr-defined]
    return exc


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(body: LoginRequest, request: Request, backgroundTasks: BackgroundTasks):
    """Authenticate a staff login and issue an access token.

    Checks run in a fixed order — user exists, password correct, account not
    locked, account active — with the password check deliberately performed
    before the lock/active checks (so a lock only triggers on a correct
    username, and to avoid revealing account state via timing). The
    `LOGIN_SUCCESS` audit write is deferred to a background task so it
    doesn't block the response.

    Raises:
        HTTPException: 401 (`INVALID_CREDENTIALS`), for an unknown username
            or wrong password (both take one bcrypt check, so timing doesn't
            reveal which usernames exist). 423 (`ACCOUNT_LOCKED`), inside the
            `LOCKOUT_MINUTES` window after too many failures. 403
            (`ACCOUNT_INACTIVE`), if the account is inactive.
        TooManyRequestsException: 429 (`TOO_MANY_LOGIN_ATTEMPTS`), if this
            username or client IP is over its login rate limit.
    """
    ipAddress = request.client.host if request.client else "unknown"
    userAgent = request.headers.get("user-agent")

    # 0. Rate limit before any lookup or password check (UROLENS-222, F-06)
    enforceLoginRateLimit("staff", body.username, ipAddress)

    # 1. User must exist
    user = await getUserByUsername(body.username)
    if user is None:
        await spendPasswordCheck(body.password)  # same timing as a wrong password
        await audit_logger.logLoginFailed(ipAddress)
        raise _apiError(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    # 2. The password is always checked (same timing either way)...
    passwordOk = await verifyPassword(body.password, user["hashed_password"])

    # 3. ...but while locked, the answer is 423 whether it was right or wrong,
    # and nothing is counted: a wrong guess can't extend the lock, and the
    # response can't tell an attacker their guess was right (UROLENS-222, F-07).
    if isLockedOut(user):
        if not passwordOk:
            await audit_logger.logLoginFailed(ipAddress, userId=user["user_id"])
        raise _apiError(
            status.HTTP_423_LOCKED,
            "ACCOUNT_LOCKED",
            f"Your account is temporarily locked. Try again in {LOCKOUT_MINUTES} minutes or contact an administrator.",
        )

    if not passwordOk:
        await asyncio.gather(
            incrementFailedAttempts(user["user_id"]),
            audit_logger.logLoginFailed(ipAddress, userId=user["user_id"]),
        )
        raise _apiError(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    # 4. Account must be active
    if not user.get("is_active", True):
        raise _apiError(status.HTTP_403_FORBIDDEN, "ACCOUNT_INACTIVE", "Your account is inactive. Contact an administrator.")

    # reset_failed_attempts and create_session are independent — run in parallel
    _, sessionRecord = await asyncio.gather(
        resetFailedAttempts(user["user_id"]),
        createSession(user["user_id"], user["role"], ipAddress, userAgent),
    )
    clearLoginRateLimit("staff", body.username)
    token = issueJwt(user["user_id"], user["username"], user["role"], sessionRecord["session_id"])

    # Audit write doesn't need to block the response
    backgroundTasks.add_task(
        audit_logger.logLoginSuccess, user["user_id"], sessionRecord["session_id"], ipAddress
    )

    return LoginResponse(accessToken=token, role=user["role"], userId=str(user["user_id"]))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, claims: dict = Depends(getCurrentUser)):
    """Close the authenticated staff session and record a `LOGOUT` audit entry."""
    ipAddress = request.client.host if request.client else "unknown"
    await closeSession(claims["session_id"])
    await audit_logger.logLogout(claims["user_id"], claims["session_id"], ipAddress)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
