"""Real-database tests — the pending migrations on a copy of live's schema, and drift.

Live is at 0048. A throwaway database is copied from `LIVE_COPY_TEMPLATE` (live's
schema at 0048, see conftest.py) and dropped afterwards:

- `alembic upgrade head` applies 0049-0053: the return pointer columns, the
  capture id column and its unique index exist, `specimens.patient_name` becomes
  TEXT, the `created_at` rename finds nothing to rename (live already has
  `rejected_at`, `dispatched_at`, `generated_at`), and every table keeps
  row-level security; 0053 (`analysis_results.smart_diagnosis`) finds the column
  already there and changes nothing;
- `alembic downgrade 0048` removes them again (0052 and 0053 are no-ops going down), and a
  second upgrade succeeds, so the deploy can be rolled back and retried;
- a 200-character name with accents is received on a copy at head (its stored
  ciphertext is over 255 characters), and fails on a copy at 0048;
- the 0051 downgrade refuses while a stored name is longer than 255 characters.

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
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401 — registers every model on Base.metadata
from src.core.encryption import encryptPii
from src.models.base import Base
from src.schemas.specimen import SpecimenReceiveRequest
from src.services import specimen_service
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

# What the website's routes use that the phone's don't: the rest of the models' tables.
WEBSITE_TABLES = (
    "engine_error_logs", "escalations", "patients", "print_jobs", "result_releases", "result_retrievals",
    "result_reviews", "result_views", "sample_labels", "smart_diagnosis_outputs", "specimen_rejections",
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
    with engine.connect() as conn:
        timestamps = {
            (r.table_name, r.column_name) for r in conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name IN"
                " ('created_at', 'rejected_at', 'dispatched_at', 'generated_at')"
                " AND table_name IN ('specimen_rejections', 'print_jobs', 'sample_labels')"
            ))
        }
    patientName = {c["name"]: c for c in inspector.get_columns("specimens")}["patient_name"]["type"]
    return {
        "version": version,
        "withoutRls": withoutRls,
        "returnPointer": {"field_id", "particle_type"} <= returns,
        "captureId": "client_capture_id" in fields,
        "captureIndex": "uq_result_fields_specimen_capture" in indexes,
        "patientNameType": type(patientName).__name__,
        "timestamps": timestamps,
    }


# What live has, and what the created_at migration must leave alone.
LIVE_TIMESTAMPS = {
    ("specimen_rejections", "rejected_at"), ("print_jobs", "dispatched_at"), ("sample_labels", "generated_at"),
}


def test_thePendingMigrationsApplyRollBackAndApplyAgainOnLive(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    assert _state(engine)["version"] == LIVE_VERSION

    applied = {
        "version": _head(), "withoutRls": 0, "returnPointer": True, "captureId": True, "captureIndex": True,
        "patientNameType": "TEXT", "timestamps": LIVE_TIMESTAMPS,
    }
    _alembic(url, "upgrade", "head")
    assert _state(engine) == applied

    _alembic(url, "downgrade", LIVE_VERSION)
    assert _state(engine) == {
        "version": LIVE_VERSION, "withoutRls": 0, "returnPointer": False, "captureId": False, "captureIndex": False,
        "patientNameType": "VARCHAR", "timestamps": LIVE_TIMESTAMPS,
    }

    _alembic(url, "upgrade", "head")
    assert _state(engine) == applied


def test_theSmartDiagnosisColumnMigrationChangesNothingOnLive(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    query = text(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
        " WHERE table_name = 'analysis_results' AND column_name = 'smart_diagnosis'"
    )
    with engine.connect() as conn:
        before = conn.execute(query).one()
    assert tuple(before) == ("jsonb", "YES", None)

    _alembic(url, "upgrade", "head")

    with engine.connect() as conn:
        assert conn.execute(query).one() == before


def _accentedName() -> tuple[str, str]:
    """A first and last name of 200 characters in all (one space between), with ñ and é."""
    return "Ñandú" + "ñ" * 54 + "é", "Peña" + "é" * 134 + "ñ"


async def _seedPendingLabRequest(session, first: str, last: str) -> SimpleNamespace:
    tag = uuid.uuid4().hex[:8]
    receptionistId, patientId, labRequestId = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (user_id, username, hashed_password, role) VALUES (:id, :u, 'x', 'RECEPTIONIST')"),
        {"id": receptionistId, "u": f"t-recv-{tag}"},
    )
    await session.execute(
        text(
            "INSERT INTO patients (patient_id, patient_uid, first_name, last_name, date_of_birth, sex,"
            " registered_by, dedup_hash) VALUES (:id, :uid, :fn, :ln, :dob, 'FEMALE', :by, :hash)"
        ),
        {"id": patientId, "uid": f"PAT-T{tag}", "fn": encryptPii(first), "ln": encryptPii(last),
         "dob": encryptPii("1990-01-01"), "by": receptionistId, "hash": f"test-{tag}"},
    )
    await session.execute(
        text(
            "INSERT INTO lab_requests (lab_request_id, request_uid, patient_id, test_type, encoded_by, status)"
            " VALUES (:id, :uid, :p, 'Urinalysis - Routine', :by, 'PENDING_SAMPLE')"
        ),
        {"id": labRequestId, "uid": f"REQ-T{tag}", "p": patientId, "by": receptionistId},
    )
    await session.commit()
    return SimpleNamespace(labRequestId=labRequestId, receptionistId=receptionistId)


async def _receive(url: URL, first: str, last: str):
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), poolclass=NullPool)
    session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)()
    try:
        ids = await _seedPendingLabRequest(session, first, last)
        response = await specimen_service.receiveSpecimen(
            session, ids.receptionistId, SpecimenReceiveRequest(labRequestId=ids.labRequestId, visualCheckPassed=True),
        )
        stored = await session.scalar(
            text("SELECT patient_name FROM specimens WHERE specimen_id = :s"), {"s": response.specimenId},
        )
        return response, stored
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_aTwoHundredCharacterAccentedNameIsReceivedOnLiveAtHead(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, _ = copyAtLiveVersion
    first, last = _accentedName()
    assert len(f"{first} {last}") == 200
    _alembic(url, "upgrade", "head")

    response, stored = await _receive(url, first, last)

    assert response.success
    assert len(stored) > 255  # the encrypted name, which VARCHAR(255) could not hold


@pytest.mark.asyncio
async def test_theSameReceiveFailsOnLiveBeforeThePatientNameIsWidened(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, _ = copyAtLiveVersion
    first, last = _accentedName()

    with pytest.raises(DBAPIError, match="value too long for type character varying"):
        await _receive(url, first, last)


def test_theWideningCannotBeRolledBackWhileANameIsLongerThan255(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    _alembic(url, "upgrade", "head")
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))  # skip FKs for this one row
        conn.execute(
            text("INSERT INTO specimens (lab_request_id, received_by, patient_name) VALUES (:l, :r, :n)"),
            {"l": uuid.uuid4(), "r": uuid.uuid4(), "n": "x" * 300},
        )

    with pytest.raises(subprocess.CalledProcessError) as raised:
        _alembic(url, "downgrade", "0050")
    assert b"0051 cannot be downgraded" in raised.value.stderr
    assert _state(engine)["version"] == _head()


def test_theCaptureIdIsUniquePerSpecimenOnlyWhenSet(copyAtLiveVersion: tuple[URL, Engine]) -> None:
    url, engine = copyAtLiveVersion
    _alembic(url, "upgrade", "head")

    [index] = [i for i in inspect(engine).get_indexes("result_fields") if i["name"] == "uq_result_fields_specimen_capture"]

    assert index["unique"]
    assert index["column_names"] == ["specimen_id", "client_capture_id"]
    assert "client_capture_id IS NOT NULL" in index["dialect_options"]["postgresql_where"]


@pytest.mark.parametrize("table", PHONE_TABLES + WEBSITE_TABLES)
def test_everyColumnTheModelsMapForThePhonesAndWebsiteTablesExistsOnLive(table: str) -> None:
    engine = create_engine(localCopyUrl().set(drivername="postgresql"))
    try:
        live = {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()

    missing = set(Base.metadata.tables[table].columns.keys()) - live
    assert not missing, f"{table}: the model maps columns live doesn't have: {sorted(missing)}"
