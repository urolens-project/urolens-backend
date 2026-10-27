"""Real-database check: an empty Postgres builds to head and matches the models.

Skipped unless `FRESH_DB_URL` points at an EMPTY scratch database (it runs
`alembic upgrade head` on it). Never point it at a shared or live database. The
URL is passed to Alembic as `-x dbUrl=...`, because `.env` (reloaded with
override=True) would otherwise win over an environment variable.
Needs the B1 baseline revisions (0000, 0046).

    FRESH_DB_URL=postgresql://user:pass@localhost:5432/scratch pytest tests/test_fresh_db_build.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.core.config import settings
from src.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
FRESH_DB_URL = os.getenv("FRESH_DB_URL")
NEW_TABLES = {"result_fields", "field_annotation_edits"}

pytestmark = pytest.mark.skipif(not FRESH_DB_URL, reason="FRESH_DB_URL not set")


@pytest.fixture(scope="module")
def engine():
    fresh, configured = make_url(FRESH_DB_URL), make_url(settings.databaseUrl)
    assert (fresh.host, fresh.port, fresh.database) != (configured.host, configured.port, configured.database), (
        "FRESH_DB_URL names the database in .env; it must be an empty scratch database"
    )
    eng = create_engine(FRESH_DB_URL)
    assert not inspect(eng).get_table_names(), "FRESH_DB_URL must point at an empty database"
    # The URL is the developer's own FRESH_DB_URL, checked above; no shell is involved.
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-x", f"dbUrl={FRESH_DB_URL}", "upgrade", "head"],
        cwd=REPO_ROOT, check=True,
    )
    yield eng
    eng.dispose()


def test_theDatabaseIsAtTheScriptHead(engine):
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    [head] = ScriptDirectory.from_config(cfg).get_heads()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head


def test_everyMappedAnalysisResultColumnExists(engine):
    # ai_detections was mapped before any migration created it (0048).
    columns = {c["name"] for c in inspect(engine).get_columns("analysis_results")}
    assert set(Base.metadata.tables["analysis_results"].columns.keys()) <= columns


def test_everyModelTableExists(engine):
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())


def test_newTablesHaveRlsEnabled(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:names)"
        ), {"names": list(NEW_TABLES)}).all()
    assert {r[0]: r[1] for r in rows} == {name: True for name in NEW_TABLES}


def test_noModelDriftOnTheMultiFieldObjects(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diffs = compare_metadata(context, Base.metadata)
    touched = [d for d in diffs if NEW_TABLES & {str(x) for x in _tables(d)}]
    assert touched == [], touched


def test_backfillOnAnEmptyDatabaseCreatesNothing(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM result_fields")).scalar() == 0


def _tables(diff):
    items = diff if isinstance(diff, list) else [diff]
    for item in items:
        for part in item:
            name = getattr(getattr(part, "table", None), "name", None) or getattr(part, "name", None)
            if isinstance(name, str):
                yield name
