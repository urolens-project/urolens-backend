"""Unit tests — migration 0048 (`analysis_results.ai_detections`).

The model maps `ai_detections`, so a database built from migrations must have it:
the revision adds it only where missing (a no-op on live, which already has it),
sits on 0047 as the single head, and its downgrade refuses to drop boxes.
No database: `upgrade()` is rendered to SQL through an offline Alembic context.
"""
from __future__ import annotations

import inspect
import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from src.models.analysis_result import AnalysisResult

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def scriptDir() -> ScriptDirectory:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def _render(function) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        function()
    return buffer.getvalue()


def test_revisionSitsOn0047AndTheChainHasOneHead(scriptDir: ScriptDirectory) -> None:
    assert scriptDir.get_revision("0048").down_revision == "0047"
    assert len(scriptDir.get_heads()) == 1


def test_upgradeAddsTheColumnOnlyWhereItIsMissing(scriptDir: ScriptDirectory) -> None:
    sql = _render(scriptDir.get_revision("0048").module.upgrade)
    assert "ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS ai_detections JSONB" in sql


def test_theColumnMatchesWhatTheModelMaps() -> None:
    column = AnalysisResult.__table__.c["ai_detections"]
    assert column.nullable is True
    assert column.type.__class__.__name__ == "JSONB"


def test_downgradeRefusesWhileAnyResultHasBoxes(scriptDir: ScriptDirectory) -> None:
    source = inspect.getsource(scriptDir.get_revision("0048").module.downgrade)
    guard = source.index("ai_detections IS NOT NULL")
    assert source.index("RAISE EXCEPTION") > guard
    assert source.index("DROP COLUMN IF EXISTS ai_detections") > source.index("RAISE EXCEPTION")
