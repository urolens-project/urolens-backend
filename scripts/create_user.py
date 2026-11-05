"""Create one staff user in the `users` table.

Why it exists: there is no endpoint for creating staff accounts, and
seed/seed_users.py creates fixed test users with a known password. This
creates a single real account with a password chosen at run time.

Inputs (each is read from an environment variable if set, else prompted):
    CREATE_USER_ROLE      ADMINISTRATOR | SUPERVISOR | MEDTECH | RECEPTIONIST | PHYSICIAN
    CREATE_USER_NAME      username (case-sensitive, max 80 chars)
    CREATE_USER_PASSWORD  password, at least 12 characters (prompted hidden, twice)
    DATABASE_URL          target database (prompted hidden if unset)

The users table has no encrypted or hashed-lookup columns (only the bcrypt
`hashed_password`), so ENCRYPTION_KEY / DEDUP_HASH_KEY are NOT used here. They
are only needed to import src.core.config, and may hold throwaway values.

This script never reads a .env file. Prints the new user id and role only.

Usage (normally via deploy/create-user.ps1, which runs it inside the image):
    python scripts/create_user.py
"""
from __future__ import annotations

import getpass
import os
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.auth_service import hashPassword
from src.models.user import User

ALLOWED_ROLES = ("ADMINISTRATOR", "SUPERVISOR", "MEDTECH", "RECEPTIONIST", "PHYSICIAN")
MIN_PASSWORD_LENGTH = 12
MAX_USERNAME_LENGTH = 80  # users.username is VARCHAR(80)
MAX_PASSWORD_BYTES = 72   # bcrypt ignores everything past 72 bytes


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _normaliseRole(raw: str) -> str:
    role = raw.strip().upper()
    if role not in ALLOWED_ROLES:
        _fail(f"Role must be one of: {', '.join(ALLOWED_ROLES)}.")
    return role


def _validateUsername(raw: str) -> str:
    if not raw or raw != raw.strip():
        _fail("Username must be non-empty and have no leading/trailing spaces.")
    if len(raw) > MAX_USERNAME_LENGTH:
        _fail(f"Username must be at most {MAX_USERNAME_LENGTH} characters.")
    return raw


def _validatePassword(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        _fail(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        _fail(f"Password must be at most {MAX_PASSWORD_BYTES} bytes (bcrypt limit).")
    return password


def _promptPasswordTwice() -> str:
    first = getpass.getpass("Password (hidden, min 12 chars): ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        _fail("Passwords do not match.")
    return first


def _toSyncUrl(url: str) -> str:
    """Normalise any postgres URL form to the psycopg2 driver."""
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    _fail("DATABASE_URL must start with postgresql:// or postgres://.")
    return url  # unreachable


def main() -> None:
    role = _normaliseRole(os.environ.get("CREATE_USER_ROLE") or input(f"Role ({'|'.join(ALLOWED_ROLES)}): "))
    username = _validateUsername(os.environ.get("CREATE_USER_NAME") or input("Username: "))
    password = _validatePassword(os.environ.get("CREATE_USER_PASSWORD") or _promptPasswordTwice())
    databaseUrl = os.environ.get("DATABASE_URL") or getpass.getpass("DATABASE_URL (hidden): ")
    if not databaseUrl:
        _fail("No DATABASE_URL given.")

    engine = create_engine(_toSyncUrl(databaseUrl), pool_pre_ping=True)
    try:
        with Session(engine) as session:
            existing = session.execute(select(User.userId).where(User.username == username)).first()
            if existing is not None:
                _fail("A user with that username already exists. Nothing was changed.")
            user = User(username=username, hashedPassword=hashPassword(password), role=role)
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                _fail("A user with that username already exists. Nothing was changed.")
            print(f"Created user id={user.userId} role={user.role}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
