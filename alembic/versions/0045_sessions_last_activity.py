"""sessions: last_activity_at column for inactivity-based expiration

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-02 00:00:00.000000

UROLENS-167: the `sessions` table (never created by a migration in this
repo — provisioned directly in Supabase, like `users`/`specimens`/
`audit_logs`; see the `supabase-alembic-migration` skill's documented
fresh-DB gap) had no concept of "last activity" at all. Session validity
was judged purely by the JWT's own fixed `exp` (set once at login, unrelated
to use) plus a boolean `is_active` revocation flag — there was no backend
enforcement of the UAC's actual inactivity timeout; a non-UI client holding
a still-valid token could stay "idle" for that token's entire absolute
lifetime with nothing to stop it.

`ADD COLUMN IF NOT EXISTS`, matching this repo's established pattern (see
migration 0032's docstring) for altering a table this codebase's migration
history never created, since the live schema can't be confirmed from this
authoring environment.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0045'
down_revision: str | Sequence[str] | None = '0044'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS last_activity_at")
