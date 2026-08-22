"""Core infrastructure: config, database/Supabase clients, auth/session
primitives, RBAC dependencies, encryption, audit logging, and shared
exceptions/enums. Public API re-exported below — prefer
`from src.urolens.core import RequireRole` over reaching into a submodule
directly (rule 6). Importing anything from this package (even `UserRole`
alone) now eagerly constructs the Settings/Supabase client/DB engine, since
Python must run this file before any submodule import completes — a
deliberate tradeoff, not an oversight; see changelog.md's barrel-indexing
entry."""
from .config import Settings, settings  # noqa: F401
from .database import AsyncSessionLocal, engine, get_db  # noqa: F401
from .supabase import get_supabase, supabase  # noqa: F401
from .audit_logger import (  # noqa: F401
    AuditLogger,
    get_audit_logger,
    log_access_denied,
    log_login_failed,
    log_login_success,
    log_logout,
    log_patient_login_failed,
    log_patient_login_success,
    log_patient_logout,
)
from .auth_service import (  # noqa: F401
    close_session,
    create_session,
    decode_jwt,
    get_user_by_username,
    hash_password,
    increment_failed_attempts,
    is_session_active,
    issue_jwt,
    reset_failed_attempts,
    verify_password,
)
from .encryption import decrypt_pii, encrypt_pii  # noqa: F401
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
from .rbac import RequireRole, get_current_user, security_scheme  # noqa: F401
