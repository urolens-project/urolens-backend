"""Unit tests — baseline revision 0000 (B1: a fresh database must build).
No database: migration source is read through Alembic's script directory.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_TABLES = {"users", "specimens", "audit_logs", "result_confirmations", "sessions"}
_CREATE_RE = re.compile(
    r"CREATE TABLE(?: IF NOT EXISTS)?\s+(?:public\.)?(\w+)|create_table\(\s*['\"](\w+)['\"]"
)


@pytest.fixture(scope="module")
def scriptDir() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_baselineIsTheOnlyRoot(scriptDir):
    assert scriptDir.get_bases() == ["0000"]
    assert scriptDir.get_revision("0004").down_revision == "0000"


def test_baselineCannotBeDowngraded(scriptDir):
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        scriptDir.get_revision("0000").module.downgrade()


def test_everyModelTableHasACreateInTheChain(scriptDir):
    created: set[str] = set()
    for rev in scriptDir.walk_revisions():
        for groups in _CREATE_RE.findall(inspect.getsource(rev.module.upgrade)):
            created |= {g.lower() for g in groups if g}
    missing = sorted(set(Base.metadata.tables) - created)
    assert missing == [], f"Model table(s) {missing} are never CREATEd by a migration."
    assert "sessions" in created


def test_baselineStatementsAreAllGuarded(scriptDir):
    src = inspect.getsource(scriptDir.get_revision("0000").module)
    assert not re.search(r"CREATE TABLE (?!IF NOT EXISTS)", src)
    assert not re.search(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", src)
    assert src.count("IF NOT EXISTS (SELECT 1 FROM pg_type") == 2  # specimen_status, user_role
    for table in BASELINE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in src
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "CREATE POLICY" not in src


@pytest.mark.parametrize(
    ("name", "table", "target"),
    [
        ("fk_specimens_lab_request", "specimens", "lab_requests(lab_request_id)"),
        ("fk_confirmations_result", "result_confirmations", "analysis_results(result_id)"),
    ],
)
def test_liveFksAreCreatedAndDroppedWithAGuard(scriptDir, name, table, target):
    rev = scriptDir.get_revision("0046")
    assert rev.down_revision == "0045"
    up = inspect.getsource(rev.module.upgrade)
    assert f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}')" in up  # noqa: S608 — assertion, not a query
    assert f"ALTER TABLE {table} ADD CONSTRAINT {name}" in up
    assert target in up
    assert "ON DELETE" not in up  # live is NO ACTION
    down = inspect.getsource(rev.module.downgrade)
    assert f"IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}')" in down  # noqa: S608 — assertion, not a query
    assert f"DROP CONSTRAINT {name}" in down


def test_singleHeadIs0046(scriptDir):
    assert scriptDir.get_heads() == ["0046"]
