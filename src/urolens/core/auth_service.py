"""Staff authentication primitives: password hashing/verification, session
rows in the `sessions` table, and JWT issuing/decoding. Used by the auth
router and by `core.rbac`'s request-authentication dependency.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import settings
from .supabase import supabase


async def verifyPassword(plainPassword: str, hashedPassword: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    Args:
        hashed_password: the stored bcrypt hash to check against.

    Returns:
        `True` if the password matches; `False` on mismatch, or if
        `hashed_password` isn't a valid bcrypt hash (e.g. plaintext was
        accidentally stored).
    """
    # bcrypt is CPU-bound and synchronous — run in a thread so the event loop
    # isn't blocked while it hashes (typically 200–400 ms at cost factor 12).
    # ValueError is raised if the stored value is not a valid bcrypt hash
    # (e.g. plaintext was accidentally stored); treat that as a failed check.
    try:
        return await asyncio.to_thread(
            bcrypt.checkpw,
            plainPassword.encode("utf-8"),
            hashedPassword.encode("utf-8"),
        )
    except ValueError:
        return False


def hashPassword(plainPassword: str) -> str:
    """Hash a plaintext password with bcrypt (a fresh random salt per call).

    Returns:
        The bcrypt hash, encoded as a UTF-8 string suitable for storage.
    """
    return bcrypt.hashpw(
        plainPassword.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


async def getUserByUsername(username: str) -> dict | None:
    """Look up a user row by username.

    Returns:
        The user row as a dict, or `None` if no user has that username.
    """
    result = await supabase.table("users").select("*").eq(
        "username", username
    ).maybe_single().execute()
    return result.data if result is not None else None


async def incrementFailedAttempts(userId) -> None:
    """Increment a user's `failed_attempts` counter after a failed login,
    locking the account (setting `locked_at`) once the count reaches
    `settings.max_failed_attempts`. No-op if the user no longer exists.
    """
    user = await _getUserById(userId)
    if not user:
        return
    newCount = user["failed_attempts"] + 1
    updateData = {"failed_attempts": newCount}
    if newCount >= settings.maxFailedAttempts:
        updateData["locked_at"] = datetime.now(UTC).isoformat()
    await supabase.table("users").update(updateData).eq(
        "user_id", str(userId)
    ).execute()


async def resetFailedAttempts(userId) -> None:
    """Clear a user's `failed_attempts` counter and any account lock,
    typically after a successful login.
    """
    await supabase.table("users").update(
        {"failed_attempts": 0, "locked_at": None}
    ).eq("user_id", str(userId)).execute()


async def createSession(
    userId, role: str, ipAddress: str | None = None, userAgent: str | None = None
) -> dict:
    """Insert a new active row into the `sessions` table for a login.

    Returns:
        The inserted session row (including its generated `session_id`), or
        `None` if the insert returned no data.
    """
    sessionData = {
        "user_id": str(userId),
        "user_role": role,
        "login_at": datetime.now(UTC).isoformat(),
        "is_active": True,
    }
    if ipAddress:
        sessionData["ip_address"] = ipAddress
    if userAgent:
        sessionData["user_agent"] = userAgent

    result = await supabase.table("sessions").insert(sessionData).execute()
    return result.data[0] if result.data else None


async def closeSession(sessionId) -> None:
    """Mark a session inactive and stamp its `logout_at`, on logout."""
    await supabase.table("sessions").update(
        {
            "is_active": False,
            "logout_at": datetime.now(UTC).isoformat(),
        }
    ).eq("session_id", str(sessionId)).execute()


def issueJwt(userId, username: str, role: str, sessionId) -> str:
    """Encode and sign an access token carrying identity/role/session claims.

    Returns:
        A JWT string signed with `settings.jwt_signing_key`, expiring after
        `settings.access_token_expire_minutes`.
    """
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "username": username,
        "role": role,
        "session_id": str(sessionId),
        "iat": now,
        "exp": now + timedelta(minutes=settings.accessTokenExpireMinutes),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


def decodeJwt(token: str) -> dict:
    """Verify and decode an access token issued by `issue_jwt`.

    Returns:
        The decoded claims dict.

    Raises:
        jwt.PyJWTError: (or a subclass, e.g. `ExpiredSignatureError`,
            `InvalidSignatureError`) if the token is malformed, expired, or
            fails signature verification.
    """
    return jwt.decode(token, settings.jwtSigningKey, algorithms=[settings.jwtAlgorithm])


async def isSessionActive(sessionId) -> bool:
    """Check whether a session is still active (i.e. not logged out/revoked).

    Returns:
        `True` only if the session row exists and its `is_active` flag is
        set; `False` for a missing session or an inactive one.
    """
    result = await supabase.table("sessions").select("is_active").eq(
        "session_id", str(sessionId)
    ).maybe_single().execute()
    if result is None or not result.data:
        return False
    return result.data.get("is_active", False)


async def _getUserById(userId) -> dict | None:
    # Look up a user row by primary key; returns None if it doesn't exist.
    result = await supabase.table("users").select("*").eq(
        "user_id", str(userId)
    ).maybe_single().execute()
    return result.data if result is not None else None
