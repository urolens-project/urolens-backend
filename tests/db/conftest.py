"""Shared fixtures for the real-database tests: a local copy of live's schema.

Everything else in the suite mocks the database, so it can't catch what only a real
schema shows: column types and defaults, enums, constraints, triggers, row locks and
`updated_at` bumps, and drift between the models and live (audit `inet`, the
`sample_labels` columns). These tests run the real services on a real `AsyncSession`
against a **local** copy of live's `public` schema. Only the AI engine and image
storage are stubbed.

Skipped unless `LIVE_COPY_URL` is set. Building the copy (read-only on live: the
schema and the one-row `alembic_version`, no patient data):

    pg_dump "$LIVE_URL" --schema-only --schema=public --no-owner --no-privileges -f live-schema.sql
    # pg_dump must be at least live's version (17). For a Postgres 16 scratch server,
    # drop the lines pg_dump 18 adds that 16 doesn't know, and the CREATE SCHEMA:
    grep -v -E '^\\(restrict|unrestrict) |^SET transaction_timeout|^CREATE SCHEMA public;' \\
        live-schema.sql > live-schema-local.sql
    createdb live_template && psql -d live_template -v ON_ERROR_STOP=1 -f live-schema-local.sql
    psql -d live_template -c "INSERT INTO alembic_version VALUES ('0048')"   # live's version
    createdb -T live_template live_copy
    alembic -x dbUrl=postgresql://postgres@127.0.0.1:55432/live_copy upgrade head

Then run:

    LIVE_COPY_URL=postgresql://postgres@127.0.0.1:55432/live_copy pytest tests/db

The migration test copies `live_template` (override with `LIVE_COPY_TEMPLATE`) into a
throwaway database of its own. The URL must name a database on this machine and not
the one in `.env`; that is checked before anything runs. Tests write rows with fresh
ids and never clean up, so use a throwaway copy.
"""
from __future__ import annotations

import io
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from PIL import Image as PILImage
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.audit_logger import AuditLogger
from src.core.config import settings
from src.core.encryption import encryptPii
from src.models.result_field import Magnification
from src.services.field_capture_service import FieldCaptureService
from src.services.notification_service import NotificationService
from src.services.result_field_service import ResultFieldService

LIVE_COPY_URL = os.getenv("LIVE_COPY_URL")
LIVE_COPY_TEMPLATE = os.getenv("LIVE_COPY_TEMPLATE", "live_template")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

requiresLiveCopy = pytest.mark.skipif(not LIVE_COPY_URL, reason="LIVE_COPY_URL not set")

# Modules that hold a Supabase client: replaced for every test here, so nothing can
# reach the real project even by accident.
_SUPABASE_HOLDERS = (
    ("src.core.audit_logger", "supabase"),
    ("src.core.storage", "supabase"),
    ("src.services.ai_integration_service", "sb"),
    ("src.services.result_review_service", "supabase"),
)


def localCopyUrl() -> URL:
    """`LIVE_COPY_URL`, after checking it is a local database and not the one in `.env`."""
    url = make_url(LIVE_COPY_URL or "")
    configured = make_url(settings.databaseUrl)
    assert url.host in _LOCAL_HOSTS, f"LIVE_COPY_URL must be on this machine, not {url.host!r}"
    assert (url.host, url.port, url.database) != (configured.host, configured.port, configured.database), (
        "LIVE_COPY_URL names the database in .env"
    )
    return url


class _RefusingSupabase:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"Real-database test reached the Supabase client (.{name})")


@pytest.fixture(autouse=True)
def noSupabase() -> Iterator[None]:
    """Fail on any use of a Supabase client; storage calls are stubbed per test."""
    patches = [patch(f"{module}.{name}", _RefusingSupabase()) for module, name in _SUPABASE_HOLDERS]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession]:
    """A session on the local copy, configured like the app's (`core.database`)."""
    engine = create_async_engine(localCopyUrl().set(drivername="postgresql+asyncpg"), poolclass=NullPool)
    session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def seedAssignedSpecimen(db: AsyncSession) -> SimpleNamespace:
    """A MedTech, a supervisor and a receptionist; a consenting patient; a lab request;
    and a specimen assigned to the MedTech, as the receptionist and queue leave it."""
    tag = uuid.uuid4().hex[:10]
    ids = SimpleNamespace(
        medtechId=uuid.uuid4(), supervisorId=uuid.uuid4(), receptionistId=uuid.uuid4(),
        patientId=uuid.uuid4(), labRequestId=uuid.uuid4(), specimenId=uuid.uuid4(),
        sampleUid=f"SMP-T{tag[:8]}", patientUid=f"PAT-T{tag[:8]}",
    )
    users = [(ids.medtechId, "MEDTECH"), (ids.supervisorId, "SUPERVISOR"), (ids.receptionistId, "RECEPTIONIST")]
    for userId, role in users:
        await db.execute(
            text("INSERT INTO users (user_id, username, hashed_password, role) VALUES (:id, :u, 'x', :r)"),
            {"id": userId, "u": f"t-{role.lower()}-{tag}", "r": role},
        )
    await db.execute(
        text(
            "INSERT INTO patients (patient_id, patient_uid, first_name, last_name, date_of_birth, sex,"
            " registered_by, dedup_hash) VALUES (:id, :uid, :fn, :ln, :dob, 'FEMALE', :by, :hash)"
        ),
        {"id": ids.patientId, "uid": ids.patientUid, "fn": encryptPii("Test"), "ln": encryptPii("Patient"),
         "dob": encryptPii("1990-01-01"), "by": ids.receptionistId, "hash": f"test-{tag}"},
    )
    await db.execute(
        text(
            "INSERT INTO consents (patient_id, consent_process, consent_storage, consent_research, recorded_by)"
            " VALUES (:p, true, true, false, :by)"
        ),
        {"p": ids.patientId, "by": ids.receptionistId},
    )
    await db.execute(
        text(
            "INSERT INTO lab_requests (lab_request_id, request_uid, patient_id, test_type, encoded_by)"
            " VALUES (:id, :uid, :p, 'Urinalysis - Routine', :by)"
        ),
        {"id": ids.labRequestId, "uid": f"REQ-T{tag[:8]}", "p": ids.patientId, "by": ids.receptionistId},
    )
    await db.execute(
        text(
            "INSERT INTO specimens (specimen_id, lab_request_id, sample_uid, status, received_by, medtech_id,"
            " assigned_at, patient_uid, test_type, priority_level)"
            " VALUES (:id, :lr, :uid, 'ASSIGNED', :by, :m, now(), :puid, 'Urinalysis - Routine', 'ROUTINE')"
        ),
        {"id": ids.specimenId, "lr": ids.labRequestId, "uid": ids.sampleUid, "by": ids.receptionistId,
         "m": ids.medtechId, "puid": ids.patientUid},
    )
    await db.execute(
        text("INSERT INTO queue_assignments (specimen_id, medtech_id, assigned_by) VALUES (:s, :m, :by)"),
        {"s": ids.specimenId, "m": ids.medtechId, "by": ids.receptionistId},
    )
    await db.commit()
    return ids


def _fieldPhoto() -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (800, 600), color=(210, 200, 180)).save(out, format="JPEG")
    return out.getvalue()


def _inference(erythrocytes: int) -> SimpleNamespace:
    boxes = [
        SimpleNamespace(class_name="erythrocytes", confidence=0.9, bbox=[20 + 40 * i, 30, 50 + 40 * i, 60])
        for i in range(erythrocytes)
    ]
    return SimpleNamespace(
        particles={"erythrocytes": erythrocytes}, confidence_scores={"erythrocytes": 0.9},
        detections=boxes, image_width=800, image_height=600,
    )


@pytest.fixture
def stubbedEngineAndStorage() -> Iterator[None]:
    """The AI engine answers 2 erythrocytes per field; storage and signed URLs do nothing."""
    with patch.object(FieldCaptureService, "_runInference", AsyncMock(return_value=_inference(2))), \
         patch.object(FieldCaptureService, "_uploadToStorage", AsyncMock()), \
         patch("src.services.result_field_service.signedImageUrls", AsyncMock(return_value={})):
        yield


async def capture(db: AsyncSession, ids: SimpleNamespace, captureId: str | None = None) -> Any:
    """`POST /fields` for the specimen, as the MedTech."""
    upload = MagicMock(content_type="image/jpeg", size=None)
    upload.read = AsyncMock(return_value=_fieldPhoto())
    service = FieldCaptureService(db=db, auditLogger=AuditLogger())
    return await service.handleFieldUpload(ids.specimenId, ids.medtechId, upload, Magnification.HPF, None, captureId)


def fieldService(db: AsyncSession) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=AuditLogger(), _notifService=NotificationService(db))
