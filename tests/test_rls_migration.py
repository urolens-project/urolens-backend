"""Unit tests — migration 0035 (SEC-0: RLS on every table) and the guard that
keeps it true for tables added later. No database: the migration's SQL is
captured by patching its `op`, and later migrations are read via Alembic's
script directory. The live-database check is `scripts/check_rls.py`.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
RLS_REVISION = "0035"

_ENABLE_RE = re.compile(
    r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+)\s+ENABLE ROW LEVEL SECURITY", re.IGNORECASE
)
_DISABLE_RE = re.compile(r"DISABLE ROW LEVEL SECURITY", re.IGNORECASE)


@pytest.fixture(scope="module")
def scriptDir() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


@pytest.fixture(scope="module")
def rlsMigration(scriptDir):
    return scriptDir.get_revision(RLS_REVISION).module


def _laterRevisions(scriptDir: ScriptDirectory):
    # Every revision applied after 0035, newest first.
    return [
        rev for rev in scriptDir.walk_revisions(base=RLS_REVISION, head="heads")
        if rev.revision != RLS_REVISION
    ]


def _runCapturingSql(migration, step: str) -> list[str]:
    fakeOp = MagicMock()
    with patch.object(migration, "op", fakeOp):
        getattr(migration, step)()
    return [c.args[0] for c in fakeOp.execute.call_args_list]


def test_rlsMigrationDirectlyFollows0034(scriptDir):
    assert scriptDir.get_revision(RLS_REVISION).down_revision == "0034"


def test_migrationHistoryStillHasASingleHead(scriptDir):
    assert len(scriptDir.get_heads()) == 1


def test_upgradeEnablesRlsOnEveryListedTable(rlsMigration):
    statements = _runCapturingSql(rlsMigration, "upgrade")
    enabled = [m.group(1) for s in statements if (m := _ENABLE_RE.search(s))]
    assert len(statements) == len(rlsMigration.RLS_TABLES)
    assert sorted(enabled) == sorted(rlsMigration.RLS_TABLES)


def test_upgradeToleratesTablesMissingFromAFreshDatabase(rlsMigration):
    # qc_reviews/sessions/users were created out-of-band; a migration-built
    # database may not have them, and the upgrade must not fail on that.
    statements = _runCapturingSql(rlsMigration, "upgrade")
    assert all("IF EXISTS" in s for s in statements)


def test_upgradeNeverForcesRlsOnTheOwningRole(rlsMigration):
    # FORCE would subject `postgres` (SQLAlchemy/Alembic's login) to RLS
    # with no policies, blocking the backend itself.
    statements = _runCapturingSql(rlsMigration, "upgrade")
    assert not any("FORCE" in s.upper() for s in statements)


def test_upgradeAddsNoPolicyOrGrant(rlsMigration):
    # No policy is the lock; GRANT is banned in migrations (backend-standards rule 13).
    statements = _runCapturingSql(rlsMigration, "upgrade")
    assert not any(re.search(r"\b(CREATE POLICY|GRANT)\b", s, re.I) for s in statements)


def test_downgradeDisablesRlsOnExactlyTheTablesUpgradeEnabled(rlsMigration):
    statements = _runCapturingSql(rlsMigration, "downgrade")
    disabled = [
        m.group(1) for s in statements
        if (m := re.search(r"ALTER TABLE IF EXISTS (\w+) DISABLE ROW LEVEL SECURITY", s))
    ]
    assert sorted(disabled) == sorted(rlsMigration.RLS_TABLES)


def test_rlsListCoversLiveTablesThatHaveNoModel(rlsMigration):
    # Live-only tables (no SQLAlchemy model) are the easiest to forget.
    assert {"sessions", "qc_reviews", "alembic_version"} <= set(rlsMigration.RLS_TABLES)


def test_everyModelTableHasRlsEnabledByAMigration(scriptDir, rlsMigration):
    enabled = set(rlsMigration.RLS_TABLES)
    for rev in _laterRevisions(scriptDir):
        enabled |= set(_ENABLE_RE.findall(inspect.getsource(rev.module.upgrade)))

    unlocked = sorted(set(Base.metadata.tables) - enabled)

    assert unlocked == [], (
        f"Model table(s) {unlocked} never get RLS enabled. Add "
        "`ALTER TABLE <name> ENABLE ROW LEVEL SECURITY` to the migration that "
        "creates them — see 0035."
    )


def test_noMigrationAfter0035DisablesRls(scriptDir):
    offenders = [
        rev.revision for rev in _laterRevisions(scriptDir)
        if _DISABLE_RE.search(inspect.getsource(rev.module.upgrade))
    ]
    assert offenders == [], f"Migration(s) {offenders} disable RLS in upgrade() — see 0035."
