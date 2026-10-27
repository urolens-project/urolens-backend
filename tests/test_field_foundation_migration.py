"""Unit tests — migration 0047 (multi-field foundation). No database: `upgrade()`
is rendered to SQL through an offline Alembic context and read as text, and the
module source is read for the guards. The real-database check is
`tests/test_fresh_db_build.py`.
"""
from __future__ import annotations

import inspect
import io
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_TABLES = ("result_fields", "field_annotation_edits")
_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\)", re.DOTALL)
_NOT_COLUMNS = ("CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK")


@pytest.fixture(scope="module")
def migration():
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_revision("0047")


@pytest.fixture(scope="module")
def upgradeSql(migration) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        migration.module.upgrade()
    return buffer.getvalue()


def _columns(sql: str, table: str) -> dict[str, bool]:
    """Column name -> nullable, from the CREATE TABLE emitted for `table`."""
    body = dict(_TABLE_RE.findall(sql))[table]
    found: dict[str, bool] = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith(_NOT_COLUMNS):
            continue
        found[line.split()[0]] = "NOT NULL" not in line
    return found


def test_revisionSitsOn0046WithASingleHead(migration):
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    scriptDir = ScriptDirectory.from_config(cfg)
    assert migration.down_revision == "0046"
    # Later revisions build on 0047; the chain must still have exactly one head.
    assert len(scriptDir.get_heads()) == 1


@pytest.mark.parametrize("table", NEW_TABLES)
def test_createTableMatchesTheModel(upgradeSql, table):
    emitted = _columns(upgradeSql, table)
    expected = {c.name: c.nullable for c in Base.metadata.tables[table].columns}
    assert emitted == expected


def test_rlsIsEnabledWithLiteralStatementsAndNoPolicies(upgradeSql, migration):
    for table in NEW_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in upgradeSql
        # tests/test_rls_migration.py finds these with a regex on the source.
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in inspect.getsource(
            migration.module.upgrade
        )
    assert "CREATE POLICY" not in upgradeSql


def test_everyModelIndexAndConstraintIsCreated(upgradeSql):
    for table in NEW_TABLES:
        mapped = Base.metadata.tables[table]
        for index in mapped.indexes:
            assert f"CREATE INDEX {index.name} ON {table}" in upgradeSql, index.name
        for constraint in mapped.constraints:
            if constraint.name:
                assert constraint.name in upgradeSql, constraint.name


def test_existingTablesGetOnlyAdditiveGuardedColumns(upgradeSql):
    for column in ("field_id", "kind"):
        assert f"ALTER TABLE manual_overrides ADD COLUMN IF NOT EXISTS {column}" in upgradeSql
    for column in ("field_count", "field_summary", "is_legacy_single_field"):
        assert f"ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS {column}" in upgradeSql
    assert "ALTER COLUMN" not in upgradeSql
    assert "DROP" not in upgradeSql


def test_preflightRefusesANonVarcharStatusColumn(upgradeSql):
    assert "RAISE EXCEPTION 'analysis_results.status is not VARCHAR" in upgradeSql
    assert upgradeSql.index("analysis_results.status is not VARCHAR") < upgradeSql.index(
        "CREATE TABLE result_fields"
    )


def test_backfillRulesAreInTheSql(upgradeSql):
    for text in (
        "COALESCE(r.model_version, 'unknown')",
        "COALESCE(r.smart_diagnosis_unavailable, false)",
        "COALESCE(i.width_px, 0)",
        "COALESCE(i.height_px, 0)",
        "WHERE i.status = 'ACTIVE'",
        "NOT EXISTS (SELECT 1 FROM result_fields f WHERE f.image_id = i.image_id)",
        "CASE WHEN r.confirmed_by IS NOT NULL THEN r.confirmed_at END",
    ):
        assert text in upgradeSql, text
    assert "CAPTURING" not in upgradeSql  # legacy results are NOT moved to CAPTURING


def test_legacyFlagCoversTheFourStatusesAndDoesNotChangeStatus(upgradeSql):
    update = re.search(
        r"UPDATE analysis_results\s+SET is_legacy_single_field = true(.*?)\n\s*\n",
        upgradeSql, re.DOTALL,
    )
    assert update, "legacy-flag UPDATE not found"
    for status in ("PENDING_CONFIRM", "IMAGE_RETAKE_REQUESTED", "FAILED", "RETURNED_FOR_CORRECTION"):
        assert status in update.group(1)
    assert "SET status" not in upgradeSql


def test_noUpdateTouchesAnalysisResultsUpdatedAt(upgradeSql):
    for statement in re.findall(r"UPDATE analysis_results.*?(?:\n\s*\n)", upgradeSql, re.DOTALL):
        assert "updated_at" not in statement.split("WHERE")[0], statement


def test_postCheckOnlyFailsForLegacyRowsThatHaveAnActiveImageAndNoField(upgradeSql):
    check = upgradeSql[upgradeSql.index("RAISE EXCEPTION '0047 backfill left") - 700:]
    assert "i.status = 'ACTIVE'" in check
    assert "r.is_legacy_single_field" in check
    assert "NOT EXISTS (SELECT 1 FROM result_fields f WHERE f.result_id = r.result_id)" in check
    assert "0047 backfill left a legacy result with an ACTIVE image and no field" in check


def test_downgradeRefusesWhenMultiFieldDataExists(migration):
    source = inspect.getsource(migration.module.downgrade)
    assert "FROM field_annotation_edits" in source
    assert "manual_overrides WHERE field_id IS NOT NULL" in source
    assert "status = 'CAPTURING'" in source
    assert source.index("RAISE EXCEPTION") < source.index("DROP COLUMN")
    assert "drop_table('field_annotation_edits')" in source
    assert "drop_table('result_fields')" in source


def test_docstringCarriesTheDryRunQueries(migration):
    doc = migration.module.__doc__
    for marker in ("D1 ", "D2 ", "D3 ", "D4 ", "D5 ", "D6 ", "D7 ", "D8 "):
        assert marker in doc, marker
    assert "NULLs the backfill COALESCEs away" in doc
