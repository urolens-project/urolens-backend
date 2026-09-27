"""set image bucket size limit

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-27

Renumbered from 0037 to 0042 while retargeting UROLENS-220 onto development
(see 0040's docstring). Content unchanged.

SEC-2 (Security & Compliance / security audit F-09). The backend now refuses
image uploads over 10 MB (`ai_integration_service.MAX_IMAGE_BYTES`, 413
`IMAGE_TOO_LARGE`); this sets the same limit on the Storage bucket so the two
layers agree. 0041 left `file_size_limit` unset on purpose until the backend
had a cap: a bucket-side limit alone would have made large uploads fail
silently (storage upload failure is non-fatal by design).

Same shape as 0041: targets `settings.supabaseImageBucket`, the value is a
bound parameter, and it's a no-op without a Supabase `storage` schema. Keep
`MAX_IMAGE_BYTES` below equal to the service's (tests/test_bucket_migration.py
checks it).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.core.config import settings

# revision identifiers, used by Alembic.
revision: str = '0042'
down_revision: str | Sequence[str] | None = '0041'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy (a migration must not change meaning if the service constant
# later does): 10 MiB.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_HAS_STORAGE = sa.text("SELECT to_regclass('storage.buckets') IS NOT NULL")
_SET_LIMIT = sa.text("UPDATE storage.buckets SET file_size_limit = :limit WHERE id = :bucket")


def _setImageBucketLimit(limit: int | None) -> None:
    # No Supabase `storage` schema (local/CI Postgres) -> nothing to change.
    bind = op.get_bind()
    if bind.execute(_HAS_STORAGE).scalar():
        bind.execute(_SET_LIMIT, {"limit": limit, "bucket": settings.supabaseImageBucket})


def upgrade() -> None:
    """Upgrade schema."""
    _setImageBucketLimit(MAX_IMAGE_BYTES)


def downgrade() -> None:
    """Downgrade schema."""
    _setImageBucketLimit(None)
