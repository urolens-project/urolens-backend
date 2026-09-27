"""enable rls on all tables

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-26

Renumbered from 0035 to 0040 while retargeting UROLENS-220 onto development:
development already had its own 0035-0039 (lab-request, patient-sex,
dedup-hash, sample-label and queue-assignment migrations), so this one
chains after its tip (0039) instead. Content unchanged.

SEC-0 (Security & Compliance / RA 10173). Every table in the live `public`
schema had Row Level Security disabled — 27 tables, confirmed against the
live database with:

    select tablename from pg_tables
    where schemaname = 'public' and rowsecurity = false;

Supabase grants the `anon` and `authenticated` roles full table privileges
by default, and RLS is the only thing that scopes them. With RLS off, anyone
holding the project's anon key (public by design) could read, modify, or
delete every row — patients, users (password hashes), sessions, audit_logs —
straight through the PostgREST API, without touching this backend.

This enables RLS on every one of those tables and deliberately adds NO
policies: with RLS on and no policy, `anon`/`authenticated` see zero rows
and every write is rejected. Nothing this project runs is affected:
  - the Supabase REST client uses the service-role key, which has BYPASSRLS;
  - SQLAlchemy and Alembic connect as `postgres`, the tables' owner, and RLS
    does not apply to a table's owner unless FORCE ROW LEVEL SECURITY is set
    (it is intentionally not set here);
  - web and mobile never call Supabase directly — only this backend.

Supersedes the per-table `DISABLE ROW LEVEL SECURITY` in 0004–0030 and the
"RLS stays disabled" note in 0032 (not edited — they are already applied).
The route-level auth dependency (backend-standards rule 2) stays as-is;
this adds the database layer under it rather than replacing it.

Table list is explicit (backend-standards rule 13), not a loop over
pg_tables. `IF EXISTS` on each because several of these were created
out-of-band and don't exist on a fresh migration-built database
(`qc_reviews` has no model or migration at all; `sessions`, `users`,
`specimens` predate 0004). `scripts/check_rls.py` catches any live table
this list misses.

Grants are intentionally untouched: rule 13 bans GRANT in migrations, which
a symmetric downgrade of a REVOKE would need. RLS alone is sufficient to
block both roles; revoking their table grants is an optional, dashboard-side
defense-in-depth step.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0040'
down_revision: str | Sequence[str] | None = '0039'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES: tuple[str, ...] = (
    "alembic_version",
    "analysis_results",
    "audit_logs",
    "consents",
    "engine_error_logs",
    "escalations",
    "images",
    "lab_requests",
    "manual_overrides",
    "notifications",
    "patients",
    "print_jobs",
    "qc_reviews",
    "queue_assignments",
    "result_approvals",
    "result_confirmations",
    "result_releases",
    "result_retrievals",
    "result_returns",
    "result_reviews",
    "result_views",
    "sample_labels",
    "sessions",
    "smart_diagnosis_outputs",
    "specimen_rejections",
    "specimens",
    "users",
)
"""Every table in the live `public` schema as of 2026-09-26. New tables get
RLS enabled in their own creating migration, not appended here."""


def upgrade() -> None:
    """Upgrade schema."""
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Downgrade schema.

    Restores the pre-0040 state — which re-exposes every table to the anon
    key. Only for rolling back a broken deploy, never as a steady state.
    """
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS {table} DISABLE ROW LEVEL SECURITY")
