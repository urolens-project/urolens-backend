"""Real-database tests — the pending migrations on a copy of live's schema, and drift.

Live is at 0048. A throwaway database is copied from `LIVE_COPY_TEMPLATE` (live's
schema at 0048, see conftest.py) and dropped afterwards:

- `alembic upgrade head` applies 0049 and 0050: the return pointer columns, the
  capture id column and its unique index exist, and every table keeps row-level
  security;
- `alembic downgrade 0048` removes them again, and a second upgrade succeeds, so
  the deploy can be rolled back and retried.

On the copy at head (`LIVE_COPY_URL`): every column the models map for the tables the
phone's routes use exists on live, so no insert or select there can name a missing
column (the `sample_labels.created_at` kind of failure).
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.models.base import Base
from tests.db.conftest import LIVE_COPY_TEMPLATE, localCopyUrl, requiresLiveCopy

pytestmark = requiresLiveCopy

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_VERSION = "0048"

# Tables the phone's routes read or write (sync, capture, decisions, submit, start,
# reject, notifications, login), plus what the website writes that sync sends back.
PHONE_TABLES = (
    "analysis_results", "audit_logs", "consents", "field_annotation_edits", "images", "lab_requests",
    "manual_overrides", "notifications", "queue_assignments", "result_approvals", "result_confirmations",
    "result_fields", "result_returns", "specimens", "users",
)


def _head() -> str:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    [head] = ScriptDirectory.from_config(cfg).get_heads()
    return head


def _alembic(url: URL, *args: str) -> None:
    # The URL is a throwaway local database checked by localCopyUrl(); no shell is involved.
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-x", f"dbUrl={url.render_as_string(hide_password=False)}", *args],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )


@pytest.fixture
def copyAtLiveVersion() -> Iterator[tuple[URL, Engine]]:
    """A new database copied from the template (live's schema at 0048), dropped after."""
    base = localCopyUrl().set(drivername="postgresql")
    name = f"migration_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{LIVE_COPY_TEMPLATE}"'))
    url = base.set(database=name)
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _state(engine: Engine) -> dict[str, object]:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        withoutRls = conn.execute(text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
        )).scalar()
    inspector = inspect(engine)
    returns = {c["name"] for c in inspector.get_columns("result_returns")}
    fields = {c["name"] for c in inspector.get_columns("result_fields")}
    indexes = {i["name"] for i in inspector.get_indexes("result_fields")}
    return {
        "version": version,
        "withoutRls": withoutRls,
        "returnPointer": {"field_id", "particle_type"} <= returns,
        "captureId": "client_capture_id" in fields,
        "captureIndex": "uq_result_fields_specimen_capture" in indexes,
    }


def test_thePendingMigrationsApplyRollBackAndApplyAgainOnLive(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    assert _state(engine)["version"] == LIVE_VERSION

    applied = {"version": _head(), "withoutRls": 0, "returnPointer": True, "captureId": True, "captureIndex": True}
    _alembic(url, "upgrade", "head")
    assert _state(engine) == applied

    _alembic(url, "downgrade", LIVE_VERSION)
    assert _state(engine) == {
        "version": LIVE_VERSION, "withoutRls": 0, "returnPointer": False, "captureId": False, "captureIndex": False,
    }

    _alembic(url, "upgrade", "head")
    assert _state(engine) == applied


def test_theCaptureIdIsUniquePerSpecimenOnlyWhenSet(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    _alembic(url, "upgrade", "head")

    [index] = [i for i in inspect(engine).get_indexes("result_fields") if i["name"] == "uq_result_fields_specimen_capture"]

    assert index["unique"]
    assert index["column_names"] == ["specimen_id", "client_capture_id"]
    assert "client_capture_id IS NOT NULL" in index["dialect_options"]["postgresql_where"]


@pytest.mark.parametrize("table", PHONE_TABLES)
def test_everyColumnTheModelsMapForThePhonesTablesExistsOnLive(table: str) -> None:
    engine = create_engine(localCopyUrl().set(drivername="postgresql"))
    try:
        live = {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()

    missing = set(Base.metadata.tables[table].columns.keys()) - live
    assert not missing, f"{table}: the model maps columns live doesn't have: {sorted(missing)}"
