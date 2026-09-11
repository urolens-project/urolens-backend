"""Single source of application config (consolidation plan row 1, Task 5).

Replaces the previously-split `app/config.py` and `src/urolens/core/config.py`
flat `os.getenv` module constants. Every secret raises `RuntimeError` at
import time (i.e. at process startup, since this module is imported before
the app finishes booting) if unset or equal to a known placeholder — the
same behavior the two prior config modules had individually, carried over
here rather than lost in the consolidation.

Deviation from the task brief, stated plainly: the brief asked for
`pydantic-settings`. That package is not installed in this environment and
this sandbox has no network access to add it, so shipping code that imports
it would fail `python -c "import main"` — a hard verification requirement.
Built instead on plain `pydantic.BaseModel` (already a dependency), populated
explicitly from `os.environ` in `_load_settings()` below rather than via
`BaseSettings`' automatic env-var binding. This gives the same outcome —
one typed, validated `Settings` object, one place secrets are checked at
startup — and is a mechanical, low-risk swap to real `pydantic-settings`
later: add the dependency, change the base class, remove `_load_settings()`'s
manual `os.getenv()` calls in favor of `BaseSettings`' field-name-to-env-var
binding. Nothing about the class's shape or the rest of the app needs to
change for that swap.

Two `DATABASE_URL` variants are deliberate, not an oversight — this
project's existing convention, preserved here: `database_url` (psycopg2-
compatible, sync) is what Alembic's migration runner needs; `async_database_url`
(asyncpg-compatible) is what the app's SQLAlchemy `AsyncEngine` needs at
runtime. Collapsing these into one would break one or the other.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(override=True)

_PLACEHOLDER_JWT_SIGNING_KEY = "change-me-in-production-use-a-long-random-string"


class Settings(BaseModel):
    """Typed, validated application configuration. Access via the module-level
    `settings` instance — never construct a second one.
    """

    # ── Supabase ──────────────────────────────────────────────────────────
    supabaseUrl: str
    supabaseServiceKey: str
    supabaseImageBucket: str = "microscopy"

    # ── Database ──────────────────────────────────────────────────────────
    databaseUrl: str
    """psycopg2-compatible sync URL — Alembic's migration runner."""
    asyncDatabaseUrl: str
    """asyncpg-compatible async URL — the app's SQLAlchemy AsyncEngine."""

    # ── Auth / JWT ────────────────────────────────────────────────────────
    jwtSigningKey: str
    jwtAlgorithm: str = "HS256"
    jwtExpiryHours: int = 8
    accessTokenExpireMinutes: int = 60
    maxFailedAttempts: int = 5

    # ── PHI encryption ────────────────────────────────────────────────────
    encryptionKey: str

    # ── AI integration ────────────────────────────────────────────────────
    aiModelVersion: str = "mvp-v1.0"

    # ── Misc ──────────────────────────────────────────────────────────────
    zeroUuid: str = "00000000-0000-0000-0000-000000000000"


def _toAsyncDatabaseUrl(syncUrl: str) -> str:
    # Rewrite a psycopg2-style DATABASE_URL to its asyncpg-driver equivalent
    # (postgresql:// / postgres:// -> postgresql+asyncpg://); left unchanged
    # if it's already in asyncpg form or uses some other scheme.
    if syncUrl.startswith("postgresql+asyncpg://"):
        return syncUrl
    if syncUrl.startswith("postgresql://"):
        return syncUrl.replace("postgresql://", "postgresql+asyncpg://", 1)
    if syncUrl.startswith("postgres://"):
        return syncUrl.replace("postgres://", "postgresql+asyncpg://", 1)
    return syncUrl


def _loadSettings() -> Settings:
    """Read every setting from the environment, validating secrets before
    the `Settings` object is even constructed.

    Raises:
        RuntimeError: any required secret is unset or still equal to its
            known placeholder value. This is intentionally raised at import
            time — before the app finishes booting — not deferred to first
            use.
    """
    databaseUrl = os.getenv("DATABASE_URL") or ""
    if not databaseUrl:
        raise RuntimeError(
            "DATABASE_URL is unset. Set a Postgres connection string in the "
            "environment before starting the app."
        )

    jwtSigningKey = os.getenv("JWT_SIGNING_KEY") or ""
    if not jwtSigningKey or jwtSigningKey == _PLACEHOLDER_JWT_SIGNING_KEY:
        raise RuntimeError(
            "JWT_SIGNING_KEY is unset or using the placeholder default. "
            "Set a strong, random JWT_SIGNING_KEY in the environment before starting the app."
        )

    supabaseUrl = os.getenv("SUPABASE_URL") or ""
    if not supabaseUrl:
        raise RuntimeError(
            "SUPABASE_URL is unset. Set your Supabase project URL in the environment "
            "before starting the app — several services still read/write via Supabase REST."
        )

    supabaseServiceKey = os.getenv("SUPABASE_SERVICE_KEY") or ""
    if not supabaseServiceKey:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY is unset. Set your Supabase service-role key in the "
            "environment before starting the app — several services still read/write via Supabase REST."
        )

    encryptionKey = os.getenv("ENCRYPTION_KEY", "")
    if not encryptionKey:
        raise RuntimeError(
            "ENCRYPTION_KEY is unset. Set a Fernet key (Fernet.generate_key()) in the "
            "environment before starting the app — PHI encryption cannot run without it."
        )
    try:
        Fernet(encryptionKey.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ENCRYPTION_KEY is set but is not a valid Fernet key. Generate one with "
            "Fernet.generate_key() and set it in the environment before starting the app."
        ) from exc

    return Settings(
        supabaseUrl=supabaseUrl,
        supabaseServiceKey=supabaseServiceKey,
        supabaseImageBucket=os.getenv("SUPABASE_IMAGE_BUCKET", "microscopy"),
        databaseUrl=databaseUrl,
        asyncDatabaseUrl=_toAsyncDatabaseUrl(databaseUrl),
        jwtSigningKey=jwtSigningKey,
        jwtAlgorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwtExpiryHours=int(os.getenv("JWT_EXPIRY_HOURS", "8")),
        accessTokenExpireMinutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")),
        maxFailedAttempts=int(os.getenv("MAX_FAILED_ATTEMPTS", "5")),
        encryptionKey=encryptionKey,
        aiModelVersion=os.getenv("AI_MODEL_VERSION", "mvp-v1.0"),
    )


# Module-level singleton — import and use this, never call _load_settings() again.
settings = _loadSettings()
