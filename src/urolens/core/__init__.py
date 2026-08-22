"""Core infrastructure: config, database/Supabase clients, auth/session
primitives, RBAC dependencies, encryption, audit logging, and shared
exceptions/enums. No public re-exports — import submodules directly
(`from src.urolens.core.rbac import RequireRole`, etc.)."""
