"""Unit tests — migration 0050 (the phone's capture id on result_fields). No database:
`upgrade()` and `downgrade()` are rendered to SQL through an offline Alembic context.

- It sits on 0049 and stays the single head.
- Upgrade only adds: a nullable `client_capture_id` and a unique index on specimen
  and capture id that ignores fields without one (older fields, older app versions),
  both guarded so the migration can run twice.
- Downgrade drops the index, then the column.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


def _scriptDir() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


@pytest.fixture(scope="module")
def migration():
    return _scriptDir().get_revision("0050")


def _render(step) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        step()
    return " ".join(buffer.getvalue().split())


def test_itSitsOn0049WithASingleHead(migration) -> None:
    assert migration.down_revision == "0049"
    assert len(_scriptDir().get_heads()) == 1


def test_upgradeAddsANullableColumnAndAPartialUniqueIndex(migration) -> None:
    sql = _render(migration.module.upgrade)

    assert "ALTER TABLE result_fields ADD COLUMN IF NOT EXISTS client_capture_id VARCHAR(64)" in sql
    assert "NOT NULL DEFAULT" not in sql
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_result_fields_specimen_capture "
        "ON result_fields (specimen_id, client_capture_id) WHERE client_capture_id IS NOT NULL"
    ) in sql
    assert "DROP" not in sql and "UPDATE" not in sql


def test_downgradeDropsTheIndexThenTheColumn(migration) -> None:
    sql = _render(migration.module.downgrade)

    dropIndex = sql.index("DROP INDEX IF EXISTS uq_result_fields_specimen_capture")
    dropColumn = sql.index("ALTER TABLE result_fields DROP COLUMN IF EXISTS client_capture_id")
    assert dropIndex < dropColumn
