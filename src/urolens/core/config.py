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

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(override=True)

_PLACEHOLDER_JWT_SIGNING_KEY = "change-me-in-production-use-a-long-random-string"


class Settings(BaseModel):
    """Typed, validated application configuration. Access via the module-level
    `settings` instance — never construct a second one.
    """

    # ── Supabase ──────────────────────────────────────────────────────────
    supabase_url: str | None = None
    supabase_service_key: str | None = None
    supabase_image_bucket: str = "microscopy"

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str
    """psycopg2-compatible sync URL — Alembic's migration runner."""
    async_database_url: str
    """asyncpg-compatible async URL — the app's SQLAlchemy AsyncEngine."""

    # ── Auth / JWT ────────────────────────────────────────────────────────
    jwt_signing_key: str
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 8
    access_token_expire_minutes: int = 60
    max_failed_attempts: int = 5

    # ── PHI encryption ────────────────────────────────────────────────────
    encryption_key: str

    # ── AI integration ────────────────────────────────────────────────────
    ai_model_version: str = "mvp-v1.0"

    # ── Misc ──────────────────────────────────────────────────────────────
    zero_uuid: str = "00000000-0000-0000-0000-000000000000"


def _to_async_database_url(sync_url: str) -> str:
    # Rewrite a psycopg2-style DATABASE_URL to its asyncpg-driver equivalent
    # (postgresql:// / postgres:// -> postgresql+asyncpg://); left unchanged
    # if it's already in asyncpg form or uses some other scheme.
    if sync_url.startswith("postgresql+asyncpg://"):
        return sync_url
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgres://"):
        return sync_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return sync_url


def _load_settings() -> Settings:
    """Read every setting from the environment, validating secrets before
    the `Settings` object is even constructed.

    Raises:
        RuntimeError: any required secret is unset or still equal to its
            known placeholder value. This is intentionally raised at import
            time — before the app finishes booting — not deferred to first
            use.
    """
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/urolens_db",
    )
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is unset. Set a Postgres connection string in the "
            "environment before starting the app."
        )

    jwt_signing_key = os.getenv("JWT_SIGNING_KEY") or ""
    if not jwt_signing_key or jwt_signing_key == _PLACEHOLDER_JWT_SIGNING_KEY:
        raise RuntimeError(
            "JWT_SIGNING_KEY is unset or using the placeholder default. "
            "Set a strong, random JWT_SIGNING_KEY in the environment before starting the app."
        )

    encryption_key = os.getenv("ENCRYPTION_KEY", "")
    if not encryption_key:
        raise RuntimeError(
            "ENCRYPTION_KEY is unset. Set a Fernet key (Fernet.generate_key()) in the "
            "environment before starting the app — PHI encryption cannot run without it."
        )

    return Settings(
        supabase_url=os.getenv("SUPABASE_URL"),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY"),
        supabase_image_bucket=os.getenv("SUPABASE_IMAGE_BUCKET", "microscopy"),
        database_url=database_url,
        async_database_url=_to_async_database_url(database_url),
        jwt_signing_key=jwt_signing_key,
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwt_expiry_hours=int(os.getenv("JWT_EXPIRY_HOURS", "8")),
        access_token_expire_minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")),
        max_failed_attempts=int(os.getenv("MAX_FAILED_ATTEMPTS", "5")),
        encryption_key=encryption_key,
        ai_model_version=os.getenv("AI_MODEL_VERSION", "mvp-v1.0"),
    )


# Module-level singleton — import and use this, never call _load_settings() again.
settings = _load_settings()
