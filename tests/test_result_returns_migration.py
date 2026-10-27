"""Unit tests — migration 0049 (`result_returns.field_id` and `particle_type`).

No database: `upgrade()` and `downgrade()` are rendered to SQL through an offline
Alembic context and read as text, and the chain is read through the script directory.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def scriptDir() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _render(function) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        function()
    return buffer.getvalue()


def test_revisionSitsOn0048AndTheChainHasOneHead(scriptDir):
    assert scriptDir.get_revision("0049").down_revision == "0048"
    assert scriptDir.get_heads() == ["0049"]


def test_upgradeAddsTheTwoColumnsTheKeyAndTheIndexAllGuarded(scriptDir):
    sql = _render(scriptDir.get_revision("0049").module.upgrade)
    assert "ALTER TABLE result_returns ADD COLUMN IF NOT EXISTS field_id UUID" in sql
    assert "ALTER TABLE result_returns ADD COLUMN IF NOT EXISTS particle_type VARCHAR(60)" in sql
    assert "WHERE conname = 'fk_result_returns_field'" in sql
    assert "FOREIGN KEY (field_id) REFERENCES result_fields(field_id) ON DELETE RESTRICT" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_result_returns_field_id ON result_returns (field_id)" in sql


def test_theNewColumnsAreNullableSoEveryExistingReturnStaysValid(scriptDir):
    sql = _render(scriptDir.get_revision("0049").module.upgrade)
    assert "NOT NULL" not in sql and "DEFAULT" not in sql
    assert "UPDATE " not in sql and "DELETE FROM" not in sql     # no backfill, nothing rewritten


def test_upgradeTouchesNothingElse(scriptDir):
    sql = _render(scriptDir.get_revision("0049").module.upgrade)
    assert "DROP" not in sql and "ALTER COLUMN" not in sql
    assert sql.count("ALTER TABLE") == 3                     # two columns and the key
    assert "ALTER TABLE result_returns" in sql and "analysis_results" not in sql


def test_downgradeRefusesWhileAReturnCarriesAPointerThenDropsGuarded(scriptDir):
    sql = _render(scriptDir.get_revision("0049").module.downgrade)
    assert "RAISE EXCEPTION '0049 downgrade refused" in sql
    assert "field_id IS NOT NULL OR particle_type IS NOT NULL" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX")
    assert "DROP INDEX IF EXISTS ix_result_returns_field_id" in sql
    assert "DROP CONSTRAINT IF EXISTS fk_result_returns_field" in sql
    assert "DROP COLUMN IF EXISTS particle_type" in sql
    assert "DROP COLUMN IF EXISTS field_id" in sql


def test_noNewTableSoNoRlsChange(scriptDir):
    sql = _render(scriptDir.get_revision("0049").module.upgrade)
    assert "CREATE TABLE" not in sql and "ROW LEVEL SECURITY" not in sql


def test_theModelMatchesTheMigration():
    table = Base.metadata.tables["result_returns"]
    assert table.columns["field_id"].nullable is True
    assert table.columns["particle_type"].nullable is True
    assert table.columns["particle_type"].type.length == 60
    foreignKey = next(iter(table.columns["field_id"].foreign_keys))
    assert foreignKey.name == "fk_result_returns_field"
    assert foreignKey.target_fullname == "result_fields.field_id"
    assert foreignKey.ondelete == "RESTRICT"
    assert any(i.name == "ix_result_returns_field_id" for i in table.indexes)
