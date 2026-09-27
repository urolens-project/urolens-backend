"""Live-database check that no data is exposed to Supabase's client-facing
roles or to the open internet (SEC-0 / migration 0040, SEC-0b / 0041).

Fails if any `public` table has Row Level Security disabled, if any RLS
policy grants access to `anon`, `authenticated`, or `public` — the roles the
project's anon key maps to — or if any Storage bucket is public. None of
these should exist: web and mobile only ever reach data through this
backend, and images only through its short-lived signed URLs.

Needs a real database, so it is NOT wired into CI (CI runs with a
placeholder DATABASE_URL). Run by hand after every migration against each
environment — as a module, from the repo root, so `src` resolves:

    python -m scripts.check_rls

Exit code is non-zero if anything is flagged.
"""
import sys

from sqlalchemy import create_engine, text

from src.core.config import settings

_CLIENT_ROLES = ("anon", "authenticated", "public")

_UNLOCKED_TABLES_SQL = text(
    "SELECT tablename FROM pg_tables "
    "WHERE schemaname = 'public' AND rowsecurity = false "
    "ORDER BY tablename"
)
_CLIENT_POLICIES_SQL = text(
    "SELECT tablename, policyname, roles::text[] FROM pg_policies "
    "WHERE schemaname = 'public' AND roles && CAST(:roles AS name[]) "
    "ORDER BY tablename, policyname"
)
# to_regclass guard: a local/CI Postgres has no Supabase `storage` schema.
_PUBLIC_BUCKETS_SQL = text(
    "SELECT id FROM storage.buckets WHERE public ORDER BY id"
)
_HAS_STORAGE_SQL = text("SELECT to_regclass('storage.buckets') IS NOT NULL")


def main() -> int:
    """Query the live schema and report any client-reachable table or bucket."""
    engine = create_engine(settings.databaseUrl)
    try:
        with engine.connect() as conn:
            unlocked = conn.execute(_UNLOCKED_TABLES_SQL).scalars().all()
            policies = conn.execute(
                _CLIENT_POLICIES_SQL, {"roles": list(_CLIENT_ROLES)}
            ).all()
            publicBuckets = (
                conn.execute(_PUBLIC_BUCKETS_SQL).scalars().all()
                if conn.execute(_HAS_STORAGE_SQL).scalar()
                else []
            )
    finally:
        engine.dispose()

    failed = False
    if unlocked:
        failed = True
        print(f"FAIL: {len(unlocked)} table(s) with RLS disabled:", file=sys.stderr)
        for table in unlocked:
            print(f"  - {table}", file=sys.stderr)
    if policies:
        failed = True
        print(
            f"FAIL: {len(policies)} policy(ies) open to client roles {_CLIENT_ROLES}:",
            file=sys.stderr,
        )
        for table, policy, roles in policies:
            print(f"  - {table}.{policy} -> {roles}", file=sys.stderr)
    if publicBuckets:
        failed = True
        print(f"FAIL: {len(publicBuckets)} public storage bucket(s):", file=sys.stderr)
        for bucket in publicBuckets:
            print(f"  - {bucket}", file=sys.stderr)
    if failed:
        return 1

    print("OK: RLS enabled on every public table, no client-role policies, no public buckets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
