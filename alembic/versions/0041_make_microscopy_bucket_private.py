"""make microscopy bucket private

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-26

Renumbered from 0036 to 0041 while retargeting UROLENS-220 onto development
(see 0040's docstring). Content unchanged.

SEC-0b (Security & Compliance / RA 10173). The `microscopy` Supabase Storage
bucket — every specimen's urine microscopy image — was public (confirmed
live: `storage.buckets.public = true`), so any image was readable by anyone
holding its `/storage/v1/object/public/...` link: no login, no expiry, no
revocation. The backend handed those links to the supervisor-review and
physician-result views.

Those views now get a short-lived signed URL from `src.core.storage.
signedImageUrl`, which works on a private bucket, so the bucket can be
closed. Also restricts the bucket to the two formats the upload endpoint
already accepts (`ai_integration_service.ALLOWED_MIME_TYPES`) — a no-op for
every legitimate upload, since the backend rejects anything else first.
`file_size_limit` is intentionally left unset: there is no backend upload
cap yet (SEC-2), and a bucket-side limit alone would make large phone
photos fail silently (storage upload failure is non-fatal by design).

Targets `settings.supabaseImageBucket` (`SUPABASE_IMAGE_BUCKET`, default
`microscopy`) — the bucket the app actually uploads to — so an environment
that renames it doesn't get a silent no-op here.

Done as a migration, not the dashboard toggle, per backend-standards rule
13 (no out-of-band hand-applied SQL). A no-op on a database with no
Supabase `storage` schema (a local/CI Postgres) or no such bucket yet. The
bucket name is a bound parameter, never interpolated into the SQL — which
also means this migration needs a live connection (no `alembic --sql`
offline mode).

Deploy order: ship the signed-URL code at the same time as (or before)
running this. Once this runs, any still-deployed build that emits
`/object/public/` links shows broken images until it's replaced.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.core.config import settings

# revision identifiers, used by Alembic.
revision: str = '0041'
down_revision: str | Sequence[str] | None = '0040'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HAS_STORAGE = sa.text("SELECT to_regclass('storage.buckets') IS NOT NULL")
_MAKE_PRIVATE = sa.text(
    "UPDATE storage.buckets "
    "SET public = false, allowed_mime_types = ARRAY['image/jpeg', 'image/png'] "
    "WHERE id = :bucket"
)
_MAKE_PUBLIC = sa.text(
    "UPDATE storage.buckets "
    "SET public = true, allowed_mime_types = NULL "
    "WHERE id = :bucket"
)


def _updateImageBucket(statement: sa.TextClause) -> None:
    # No Supabase `storage` schema (local/CI Postgres) -> nothing to change.
    bind = op.get_bind()
    if bind.execute(_HAS_STORAGE).scalar():
        bind.execute(statement, {"bucket": settings.supabaseImageBucket})


def upgrade() -> None:
    """Upgrade schema."""
    _updateImageBucket(_MAKE_PRIVATE)


def downgrade() -> None:
    """Downgrade schema.

    Restores the pre-0041 state — which makes every microscopy image public
    again. Only for rolling back a broken deploy, never as a steady state.
    """
    _updateImageBucket(_MAKE_PUBLIC)
