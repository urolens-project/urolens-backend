"""Staff authentication primitives: password hashing/verification, session
rows in the `sessions` table (including inactivity-based expiration,
UROLENS-167), and JWT issuing/decoding. Used by the auth router and by
`core.rbac`'s request-authentication dependency.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import settings
from .supabase import supabase

LOCKOUT_MINUTES = 15
"""How long an account stays locked after `maxFailedAttempts` failures. Once
it expires, the next attempt is allowed; a further failure locks it again
(the failure count isn't reset until a successful login), so a locked
account gets one guess per window (UROLENS-222, audit F-07)."""

# Per-role inactivity timeout (UROLENS-167): how long a session may go
# without an authenticated request before it's treated as expired,
# independent of the JWT's own fixed exp. MedTech gets a longer window
# (matches the UAC's own per-role warning timers); every other role
# (including PATIENT) gets the shorter default.
_MEDTECH_IDLE_TIMEOUT_MINUTES = 60
_DEFAULT_IDLE_TIMEOUT_MINUTES = 30


def _idleTimeoutMinutesForRole(role: str | None) -> int:
    if (role or "").upper() == "MEDTECH":
        return _MEDTECH_IDLE_TIMEOUT_MINUTES
    return _DEFAULT_IDLE_TIMEOUT_MINUTES

# bcrypt hash (cost 12, same as stored passwords) of a random value nobody
# knows — checked against when a login names no account, so an unknown
# username takes as long as a wrong password and doesn't reveal which
# usernames exist (audit F-07).
_TIMING_DUMMY_HASH = "$2b$12$g1pysAOpyNunXLw.kD7qIeV64YBwMj1TWBkhNgA6AMy3o3pFTAvcq"


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


async def spendPasswordCheck(plainPassword: str) -> None:
    """Run one bcrypt check that always fails, to equalize login timing.

    Call it on the "no such account" path so that response takes as long as
    a real password check.
    """
    await verifyPassword(plainPassword, _TIMING_DUMMY_HASH)


def isLockedOut(user: dict, now: datetime | None = None) -> bool:
    """Whether a user row is inside its lockout window.

    `locked_at` older than `LOCKOUT_MINUTES` no longer blocks — the lock
    expires on its own instead of needing an administrator, so anyone who
    knows a username can't keep that person locked out.

    Args:
        user: a `users` row as returned by Supabase (`locked_at` is an ISO
            timestamp string or `None`).
        now: current time; injectable for tests.
    """
    lockedAt = user.get("locked_at")
    if lockedAt is None:
        return False
    if isinstance(lockedAt, str):
        lockedAt = datetime.fromisoformat(lockedAt)
    now = now or datetime.now(UTC)
    return now - lockedAt < timedelta(minutes=LOCKOUT_MINUTES)


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
    now = datetime.now(UTC).isoformat()
    sessionData = {
        "user_id": str(userId),
        "user_role": role,
        "login_at": now,
        "last_activity_at": now,  # UROLENS-167: idle clock starts at login
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
    """Check whether a session is still active: not logged out/revoked, AND
    not idle past its role's inactivity timeout (UROLENS-167).

    The JWT's own `exp` is fixed at issuance and has nothing to do with
    activity — without this check, a modified/non-UI client holding a
    still-valid token could keep an idle session alive for that token's
    entire absolute lifetime, regardless of how long it's actually been
    idle. This is the backend half of "activity resets the inactivity
    timer"; the frontend's job is tracking client-side activity and
    showing the warning/countdown/redirect.

    A session found past its timeout is revoked here (same effect as an
    explicit logout) rather than merely reported expired, so a second check
    against the same stale session answers the same way. A session that's
    still within its timeout has this call's own request recorded as its
    new last activity.

    Returns:
        `True` only if the session row exists, is active, and its last
        activity (or, for a session that predates this column, its login)
        is within `_idle_timeout_minutes_for_role`'s window for its role;
        `False` otherwise.
    """
    result = await supabase.table("sessions").select(
        "is_active, last_activity_at, login_at, user_role"
    ).eq("session_id", str(sessionId)).maybe_single().execute()
    if result is None or not result.data:
        return False
    row = result.data
    if not row.get("is_active", False):
        return False

    lastActivity = row.get("last_activity_at") or row.get("login_at")
    if lastActivity is None:
        # No timestamp at all to judge staleness against (shouldn't happen
        # for any session created after this fix) -- fail closed, not open.
        return False
    if isinstance(lastActivity, str):
        lastActivity = datetime.fromisoformat(lastActivity)

    now = datetime.now(UTC)
    idleTimeout = timedelta(minutes=_idleTimeoutMinutesForRole(row.get("user_role")))
    if now - lastActivity > idleTimeout:
        await closeSession(sessionId)
        return False

    await supabase.table("sessions").update(
        {"last_activity_at": now.isoformat()}
    ).eq("session_id", str(sessionId)).execute()
    return True


async def _getUserById(userId) -> dict | None:
    # Look up a user row by primary key; returns None if it doesn't exist.
    result = await supabase.table("users").select("*").eq(
        "user_id", str(userId)
    ).maybe_single().execute()
    return result.data if result is not None else None
