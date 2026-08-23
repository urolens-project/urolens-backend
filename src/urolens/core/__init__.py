"""Core infrastructure: config, database/Supabase clients, auth/session
primitives, RBAC dependencies, encryption, audit logging, and shared
exceptions/enums. Public API re-exported below — prefer
`from src.urolens.core import RequireRole` over reaching into a submodule
directly (rule 6). Importing anything from this package (even `UserRole`
alone) now eagerly constructs the Settings/Supabase client/DB engine, since
Python must run this file before any submodule import completes — a
deliberate tradeoff, not an oversight; see changelog.md's barrel-indexing
entry.
"""
from .audit_logger import (  # noqa: F401
    AuditLogger,
    getAuditLogger,
    logAccessDenied,
    logLoginFailed,
    logLoginSuccess,
    logLogout,
    logPatientLoginFailed,
    logPatientLoginSuccess,
    logPatientLogout,
)
from .auth_service import (  # noqa: F401
    closeSession,
    createSession,
    decodeJwt,
    getUserByUsername,
    hashPassword,
    incrementFailedAttempts,
    isSessionActive,
    issueJwt,
    resetFailedAttempts,
    verifyPassword,
)
from .config import Settings, settings  # noqa: F401
from .database import AsyncSessionLocal, engine, getDb  # noqa: F401
from .encryption import decryptPii, encryptPii  # noqa: F401
from .enums import UserRole  # noqa: F401
from .exceptions import (  # noqa: F401
    ConflictError,
    ConflictException,
    ImageFormatError,
    ImageResolutionError,
    NotFoundError,
    NotFoundException,
    SpecimenNotFoundError,
    StorageError,
    UnprocessableException,
)
from .rbac import RequireRole, getCurrentUser, securityScheme  # noqa: F401
from .supabase import getSupabase, supabase  # noqa: F401
