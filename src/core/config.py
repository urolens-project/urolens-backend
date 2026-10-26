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

import importlib.util
import os
from pathlib import Path

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
    """Longest a staff session can last — one shift (UROLENS-244). A "keep me
    signed in" token lasts this long from login; refreshing never goes past it."""
    accessTokenExpireMinutes: int = 60
    """Lifetime of a normal access token. `POST /auth/refresh` renews it while the
    user is active, up to `jwtExpiryHours` after login."""
    maxFailedAttempts: int = 5

    # ── PHI encryption ────────────────────────────────────────────────────
    encryptionKey: str
    dedupHashKey: str
    """HMAC secret for the patient dedup-hash column — deliberately separate
    from `encryptionKey` (Fernet, reversible) since this key drives a
    one-way, keyed fingerprint used for exact-match duplicate lookups, not
    encryption."""

    # ── AI integration ────────────────────────────────────────────────────
    aiModelVersion: str = "mvp-v1.0"
    modelWeightsPath: str = ""
    """YOLOv8 detector weights the AI engine loads (env `MODEL_WEIGHTS_PATH`).
    Falls back to `urolens_ai/models/yolov8/best.pt` (or the older `weights.pt`)
    inside the installed package. The engine's install currently ships no data
    files, so production must set this. Empty means every upload fails with
    `AI_ANALYSIS_FAILED`."""
    gateWeightsPath: str = ""
    """Input-gate (slide / not-slide) classifier weights (env `GATE_WEIGHTS_PATH`).
    Falls back to `urolens_ai/models/gate/gate.pt` inside the installed package.
    An engine that includes the gate fails every upload without it."""
    ruleEngineConfigPath: str = ""
    """Smart Diagnosis rule configuration (env `RULE_ENGINE_CONFIG_PATH`). Falls
    back to `urolens_ai/smart_diagnosis/config.yaml` inside the installed package.
    Without it every Smart Diagnosis is marked unavailable."""
    classThresholdsPath: str = ""
    """Per-class confidence thresholds (env `CLASS_THRESHOLDS_PATH`). Falls back to
    `urolens_ai/models/yolov8/thresholds.yaml`. Optional: without it the engine
    uses one confidence threshold for every class."""

    # ── Multi-field workflow ──────────────────────────────────────────────
    fieldWorkflowEnabled: bool = False
    """Master switch (env `FIELD_WORKFLOW_ENABLED`). Off: the multi-field routes
    answer 404 `FEATURE_DISABLED` and nothing else changes. Read once at startup;
    flipping it needs a restart."""
    minFieldsPerSpecimen: int = 10
    """Accepted fields needed to submit and to confirm (env `MIN_FIELDS_PER_SPECIMEN`)."""
    fieldLowConfidenceThreshold: float = 0.5
    """A field whose mean confidence is below this is flagged LOW_CONFIDENCE."""
    fieldOutlierMultiplier: float = 2.0
    fieldOutlierMinAbove: int = 5
    """A field's class count is an outlier when it is more than
    `fieldOutlierMultiplier` times the specimen mean AND at least
    `fieldOutlierMinAbove` above it."""

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


def _bundledEngineFile(*parts: str) -> str:
    # The engine's own defaults (`src/urolens_ai/...`, relative to the working
    # directory) only exist inside the engine's repo, so from the backend they
    # never resolve. Use a file placed inside the installed package instead, if
    # there is one (found without importing the package, which reads these
    # paths at import time).
    spec = importlib.util.find_spec("urolens_ai")
    if spec is None or not spec.submodule_search_locations:
        return ""
    path = Path(next(iter(spec.submodule_search_locations))).joinpath(*parts)
    return str(path) if path.is_file() else ""


def _bundledModelWeightsPath() -> str:
    # The engine renamed its weights from weights.pt to best.pt; installs that
    # still carry the old file keep working.
    return (
        _bundledEngineFile("models", "yolov8", "best.pt")
        or _bundledEngineFile("models", "yolov8", "weights.pt")
    )


def _bundledGateWeightsPath() -> str:
    return _bundledEngineFile("models", "gate", "gate.pt")


def _bundledRuleEngineConfigPath() -> str:
    return _bundledEngineFile("smart_diagnosis", "config.yaml")


def _bundledClassThresholdsPath() -> str:
    return _bundledEngineFile("models", "yolov8", "thresholds.yaml")


def _resolveEnginePath(envName: str, bundled: str) -> str:
    # An explicit path wins; otherwise the bundled file, if any. Whatever is
    # chosen is written back to the environment, because the engine reads these
    # variables itself, once, when its modules are first imported.
    path = os.getenv(envName) or bundled
    if path:
        os.environ[envName] = path
    return path


# Engine files every upload or every Smart Diagnosis needs: (env var, setting).
# The class thresholds file is optional, so it is not listed.
_REQUIRED_ENGINE_FILES = (
    ("MODEL_WEIGHTS_PATH", "modelWeightsPath"),
    ("GATE_WEIGHTS_PATH", "gateWeightsPath"),
    ("RULE_ENGINE_CONFIG_PATH", "ruleEngineConfigPath"),
)


def engineFileProblems(current: Settings) -> list[str]:
    """List the AI engine files that are missing, one sentence each.

    Run at startup (see `main.py`), which logs each problem as an error. It
    does not stop the app: the engine is only needed for image uploads and
    Smart Diagnosis, and development or CI may run without its files.

    Args:
        current: the settings to check, normally the module's `settings`.

    Returns:
        An empty list when every required file exists.
    """
    problems = []
    for envName, attribute in _REQUIRED_ENGINE_FILES:
        path = getattr(current, attribute)
        if not path:
            problems.append(
                f"{envName} is unset and the installed AI engine has no bundled file."
            )
        elif not Path(path).is_file():
            problems.append(f"{envName} points to a file that does not exist: {path}")
    return problems


_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def _envBool(name: str, default: bool) -> bool:
    # Unset or blank means the default. Anything that is not an obvious yes/no is
    # an error, so a typo in a feature flag cannot silently leave it off (or on).
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise RuntimeError(
        f"{name} must be one of true/false/1/0/yes/no/on/off, got {raw!r}."
    )


def _envNumber(name: str, default: float, cast: type, low: float, lowInclusive: bool = True) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}.") from exc
    if value < low or (value == low and not lowInclusive):
        bound = ">=" if lowInclusive else ">"
        raise RuntimeError(f"{name} must be {bound} {low}, got {raw!r}.")
    return value


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

    dedupHashKey = os.getenv("DEDUP_HASH_KEY", "")
    if not dedupHashKey:
        raise RuntimeError(
            "DEDUP_HASH_KEY is unset. Set a long, random secret in the environment "
            "before starting the app — the patient duplicate-check hash cannot run without it."
        )

    lowConfidence = _envNumber("FIELD_LOW_CONFIDENCE_THRESHOLD", 0.5, float, 0.0, lowInclusive=False)
    if lowConfidence > 1:
        raise RuntimeError(f"FIELD_LOW_CONFIDENCE_THRESHOLD must be <= 1, got {lowConfidence}.")

    return Settings(
        fieldWorkflowEnabled=_envBool("FIELD_WORKFLOW_ENABLED", False),
        minFieldsPerSpecimen=int(_envNumber("MIN_FIELDS_PER_SPECIMEN", 10, int, 1)),
        fieldLowConfidenceThreshold=lowConfidence,
        fieldOutlierMultiplier=float(_envNumber("FIELD_OUTLIER_MULTIPLIER", 2.0, float, 1.0, lowInclusive=False)),
        fieldOutlierMinAbove=int(_envNumber("FIELD_OUTLIER_MIN_ABOVE", 5, int, 1)),
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
        dedupHashKey=dedupHashKey,
        aiModelVersion=os.getenv("AI_MODEL_VERSION", "mvp-v1.0"),
        modelWeightsPath=_resolveEnginePath("MODEL_WEIGHTS_PATH", _bundledModelWeightsPath()),
        gateWeightsPath=_resolveEnginePath("GATE_WEIGHTS_PATH", _bundledGateWeightsPath()),
        ruleEngineConfigPath=_resolveEnginePath(
            "RULE_ENGINE_CONFIG_PATH", _bundledRuleEngineConfigPath()
        ),
        classThresholdsPath=_resolveEnginePath(
            "CLASS_THRESHOLDS_PATH", _bundledClassThresholdsPath()
        ),
    )


# Module-level singleton — import and use this, never call _load_settings() again.
settings = _loadSettings()
