"""Unit tests — migrations 0041 (SEC-0b: microscopy bucket private) and 0042
(SEC-2: bucket size limit). No database: the migration's connection is
mocked and every statement it executes is captured. The live-database check
is `scripts/check_rls.py`.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.services.ai_integration_service import ALLOWED_MIME_TYPES, MAX_IMAGE_BYTES

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bucketMigration():
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_revision("0041")


def _run(migration, step: str, hasStorageSchema: bool = True) -> list[tuple[str, dict | None]]:
    """Run `step` against a mocked connection; return (sql, params) for every
    statement after the storage-schema probe."""
    bind = MagicMock()
    bind.execute.return_value.scalar.return_value = hasStorageSchema
    fakeOp = MagicMock()
    fakeOp.get_bind.return_value = bind
    with patch.object(migration.module, "op", fakeOp):
        getattr(migration.module, step)()
    calls = [(str(c.args[0]), c.args[1] if len(c.args) > 1 else None) for c in bind.execute.call_args_list]
    assert "to_regclass('storage.buckets')" in calls[0][0]  # probe always runs first
    return calls[1:]


def test_bucketMigrationFollowsTheRlsMigration(bucketMigration):
    assert bucketMigration.down_revision == "0040"


def test_upgradeMakesTheImageBucketPrivate(bucketMigration):
    [(sql, params)] = _run(bucketMigration, "upgrade")
    assert "SET public = false" in sql
    assert params == {"bucket": "microscopy"}


def test_upgradeTargetsTheBucketTheAppIsConfiguredToUpload(bucketMigration):
    # A renamed SUPABASE_IMAGE_BUCKET must not turn this into a silent no-op.
    with patch.object(bucketMigration.module.settings, "supabaseImageBucket", "microscopy-staging"):
        [(_, params)] = _run(bucketMigration, "upgrade")
    assert params == {"bucket": "microscopy-staging"}


def test_bucketNameIsBoundNotInterpolatedIntoTheSql(bucketMigration):
    hostile = "x'; DROP TABLE users; --"
    with patch.object(bucketMigration.module.settings, "supabaseImageBucket", hostile):
        [(sql, params)] = _run(bucketMigration, "upgrade")
    assert hostile not in sql
    assert ":bucket" in sql
    assert params == {"bucket": hostile}


@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_migrationChangesNothingWhereThereIsNoSupabaseStorageSchema(bucketMigration, step):
    # Local/CI Postgres has no `storage` schema; the migration must not fail there.
    assert _run(bucketMigration, step, hasStorageSchema=False) == []


def test_upgradeBucketMimeAllowlistMatchesWhatTheUploadEndpointAccepts(bucketMigration):
    # If these drift, valid uploads get silently dropped at the bucket
    # (storage upload failure is non-fatal in ai_integration_service).
    [(sql, _)] = _run(bucketMigration, "upgrade")
    assert set(re.findall(r"'(image/[a-z]+)'", sql)) == ALLOWED_MIME_TYPES


def test_upgradeSetsNoBucketSizeLimitUntilTheBackendHasOne(bucketMigration):
    [(sql, _)] = _run(bucketMigration, "upgrade")
    assert "file_size_limit" not in sql


def test_downgradeRestoresThePublicBucketWithNoMimeAllowlist(bucketMigration):
    [(sql, params)] = _run(bucketMigration, "downgrade")
    assert "SET public = true" in sql
    assert "allowed_mime_types = NULL" in sql
    assert params == {"bucket": "microscopy"}


# ── 0042: bucket size limit (SEC-2, F-09) ─────────────────────────────────────

@pytest.fixture(scope="module")
def sizeLimitMigration():
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_revision("0042")


def test_sizeLimitMigrationFollowsTheBucketMigration(sizeLimitMigration):
    assert sizeLimitMigration.down_revision == "0041"


def test_bucketSizeLimitEqualsTheUploadEndpointsCap(sizeLimitMigration):
    # If these drift, uploads between the two limits pass the backend and are
    # then silently dropped by the bucket (storage failure is non-fatal).
    [(sql, params)] = _run(sizeLimitMigration, "upgrade")
    assert "file_size_limit = :limit" in sql
    assert params == {"limit": MAX_IMAGE_BYTES, "bucket": "microscopy"}


def test_bucketSizeLimitTargetsTheConfiguredImageBucket(sizeLimitMigration):
    with patch.object(sizeLimitMigration.module.settings, "supabaseImageBucket", "microscopy-staging"):
        [(_, params)] = _run(sizeLimitMigration, "upgrade")
    assert params["bucket"] == "microscopy-staging"


@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_sizeLimitMigrationChangesNothingWithoutAStorageSchema(sizeLimitMigration, step):
    assert _run(sizeLimitMigration, step, hasStorageSchema=False) == []


def test_sizeLimitDowngradeRemovesTheLimit(sizeLimitMigration):
    [(_, params)] = _run(sizeLimitMigration, "downgrade")
    assert params["limit"] is None
