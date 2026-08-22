"""Shared enums used across the app's domain and auth layers."""
from enum import StrEnum


class UserRole(StrEnum):
    """The set of roles a session can hold. Used for RBAC checks (`core.rbac.RequireRole`)
    and stored on `users`/session rows."""

    ADMINISTRATOR = "ADMINISTRATOR"
    MEDTECH = "MEDTECH"
    PHYSICIAN = "PHYSICIAN"
    RECEPTIONIST = "RECEPTIONIST"
    SUPERVISOR = "SUPERVISOR"
    PATIENT = "PATIENT"
