from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import JWT_ALGORITHM, JWT_EXPIRY_HOURS, JWT_SIGNING_KEY, MAX_FAILED_ATTEMPTS, ZERO_UUID
from app.db.supabase import supabase


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(
        plain_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


async def get_user_by_username(username: str) -> dict | None:
    result = await supabase.table("users").select("*").eq(
        "username", username
    ).maybe_single().execute()
    return result.data


async def increment_failed_attempts(user_id) -> None:
    user = await _get_user_by_id(user_id)
    if not user:
        return
    new_count = user["failed_attempts"] + 1
    update_data = {"failed_attempts": new_count}
    if new_count >= MAX_FAILED_ATTEMPTS:
        update_data["locked_at"] = datetime.now(timezone.utc).isoformat()
    await supabase.table("users").update(update_data).eq(
        "user_id", str(user_id)
    ).execute()


async def reset_failed_attempts(user_id) -> None:
    await supabase.table("users").update(
        {"failed_attempts": 0, "locked_at": None}
    ).eq("user_id", str(user_id)).execute()


async def create_session(user_id, role: str, ip_address: str = None, user_agent: str = None) -> dict:
    session_data = {
        "user_id": str(user_id),
        "user_role": role,
        "login_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
    }
    if ip_address:
        session_data["ip_address"] = ip_address
    if user_agent:
        session_data["user_agent"] = user_agent

    result = await supabase.table("sessions").insert(session_data).execute()
    return result.data[0] if result.data else None


async def close_session(session_id) -> None:
    await supabase.table("sessions").update(
        {
            "is_active": False,
            "logout_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("session_id", str(session_id)).execute()


def issue_jwt(user_id, role: str, session_id) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": str(user_id),
        "role": role,
        "session_id": str(session_id),
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SIGNING_KEY, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    return jwt.decode(token, JWT_SIGNING_KEY, algorithms=[JWT_ALGORITHM])


async def is_session_active(session_id) -> bool:
    result = await supabase.table("sessions").select("is_active").eq(
        "session_id", str(session_id)
    ).maybe_single().execute()
    if not result.data:
        return False
    return result.data.get("is_active", False)


async def _get_user_by_id(user_id) -> dict | None:
    result = await supabase.table("users").select("*").eq(
        "user_id", str(user_id)
    ).maybe_single().execute()
    return result.data
