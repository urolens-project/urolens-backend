"""Integration tests — STORY-WEB-15: Result Releasing (T4.1)

Covers
------
1. PHYSICAL release — 201, result_releases row created, status = RELEASED,
   no notifications, audit_log has RESULT_RELEASED
2. DIGITAL release — same as above plus notification rows for patient and physician
3. Not APPROVED — result in wrong status → 422, code = RESULT_NOT_APPROVED
4. Already released — release twice → 422, code = ALREADY_RELEASED
5. RBAC — SUPERVISOR JWT on GET /api/v1/results/approved → 403

Architecture
------------
Service-layer tests (scenarios 1-4) mock a SQLAlchemy `AsyncSession` (mirrors
tests/test_lab_request_service.py's pattern): `db.get(Model, id)` is backed by
a lookup table, `db.execute()` backs the `ResultRelease` existing-release
check, and `db.flush()` assigns the generated `release_id`/`released_at` the
way a real flush against Postgres would. No real DB or network required.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from main import app
from src.core.config import settings
from src.models.analysis_result import AnalysisResult
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.models.result_release import ResultRelease
from src.models.specimen import Specimen
from src.services.result_releasing_service import ResultReleasingService

# ── Fixed IDs ─────────────────────────────────────────────────────────────────

RECEPTIONIST_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
TEST_PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000021")
TEST_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000022")
TEST_PATIENT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000023")
TEST_PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000024")
TEST_LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000025")
TEST_RELEASE_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")


# ── Row builders ─────────────────────────────────────────────────────────────

def _analysisResultRow(resultId=None, resultStatus="APPROVED", patientId=None, specimenId=None):
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = resultId or TEST_RESULT_ID
    ar.status = resultStatus
    ar.patientId = patientId if patientId is not None else TEST_PATIENT_ID
    ar.specimenId = specimenId if specimenId is not None else TEST_SPECIMEN_ID
    ar.updatedAt = datetime.now(UTC)
    ar.releasedAt = None
    return ar


def _specimenRow(specimenId=None, labRequestId=None):
    s = MagicMock(spec=Specimen)
    s.specimenId = specimenId or TEST_SPECIMEN_ID
    s.labRequestId = labRequestId if labRequestId is not None else TEST_LAB_REQUEST_ID
    s.status = "ASSIGNED"
    s.completedAt = None
    s.sampleUid = "SAMP-001"
    s.testType = "URINALYSIS"
    return s


def _patientRow(patientId=None, userId=None):
    p = MagicMock(spec=Patient)
    p.patientId = patientId or TEST_PATIENT_ID
    p.userId = userId if userId is not None else TEST_PATIENT_USER_ID
    return p


def _labRequestRow(labRequestId=None, physicianId=None):
    lr = MagicMock(spec=LabRequest)
    lr.labRequestId = labRequestId or TEST_LAB_REQUEST_ID
    lr.physicianId = physicianId if physicianId is not None else TEST_PHYSICIAN_ID
    return lr


# ── Mock AsyncSession builder ─────────────────────────────────────────────────

def _makeDb(getMap: dict, existingReleaseId=None) -> AsyncMock:
    """`db.get(Model, id)` is backed by `getMap` (keyed by `(Model, id)`).
    `db.execute()` backs the `ResultRelease` existing-release-for-this-result
    check — `existing_release_id` controls whether it reports a collision.
    `db.flush()` assigns `release_id`/`released_at` the way a real flush
    against Postgres (server-generated defaults) would.
    """
    db = AsyncMock()

    async def _get(model, id_):
        return getMap.get((model, id_))

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()

    existingResult = MagicMock()
    existingResult.scalar_one_or_none.return_value = existingReleaseId
    db.execute = AsyncMock(return_value=existingResult)

    async def _flush(objs):
        for obj in objs:
            if isinstance(obj, ResultRelease):
                obj.releaseId = TEST_RELEASE_ID
                obj.releasedAt = datetime.now(UTC)

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _makeService(db: AsyncMock) -> tuple[ResultReleasingService, MagicMock, MagicMock]:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    _notificationService = MagicMock()
    _notificationService.notify = AsyncMock()
    _service = ResultReleasingService(
        db=db,
        auditLogger=auditLogger,
        _notificationService=_notificationService,
    )
    return _service, auditLogger, _notificationService


def _currentUser(userId=None, role="RECEPTIONIST"):
    return {"user_id": str(userId or RECEPTIONIST_ID), "role": role}


def _fakeRequest():
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


# ── Scenario 1: PHYSICAL release ─────────────────────────────────────────────

class TestPhysicalRelease:
    @pytest.mark.asyncio
    async def test_physicalReleaseReturnsResponse(self):
        ar = _analysisResultRow()
        spec = _specimenRow()
        getMap = {
            (AnalysisResult, TEST_RESULT_ID): ar,
            (Specimen, TEST_SPECIMEN_ID): spec,
        }
        db = _makeDb(getMap)
        _service, auditLogger, _notificationService = _makeService(db)

        response = await _service.releaseResult(
            TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
        )

        assert response.resultId == TEST_RESULT_ID
        assert response.releaseMethod == "PHYSICAL"
        assert response.releaseId == TEST_RELEASE_ID
        assert ar.status == "RELEASED"
        assert spec.status == "COMPLETED"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_physicalReleaseNoNotificationsSent(self):
        ar = _analysisResultRow()
        spec = _specimenRow()
        getMap = {
            (AnalysisResult, TEST_RESULT_ID): ar,
            (Specimen, TEST_SPECIMEN_ID): spec,
        }
        db = _makeDb(getMap)
        _service, auditLogger, _notificationService = _makeService(db)

        await _service.releaseResult(
            TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
        )

        _notificationService.notify.assert_not_awaited()
        auditLogger.record.assert_awaited_once()
        args = auditLogger.record.call_args
        assert args[0][0] == "RESULT_RELEASED"
        assert args[1]["entityType"] == "result_release"
        assert args[1]["detailJson"]["release_method"] == "PHYSICAL"


# ── Scenario 2: DIGITAL release ───────────────────────────────────────────────

class TestDigitalRelease:
    @pytest.mark.asyncio
    async def test_digitalReleaseNotifiesPatientAndPhysician(self):
        ar = _analysisResultRow()
        spec = _specimenRow()
        patient = _patientRow()
        labRequest = _labRequestRow()
        getMap = {
            (AnalysisResult, TEST_RESULT_ID): ar,
            (Specimen, TEST_SPECIMEN_ID): spec,
            (Patient, TEST_PATIENT_ID): patient,
            (LabRequest, TEST_LAB_REQUEST_ID): labRequest,
        }
        db = _makeDb(getMap)
        _service, auditLogger, _notificationService = _makeService(db)

        await _service.releaseResult(
            TEST_RESULT_ID, "DIGITAL", _currentUser(), _fakeRequest()
        )

        assert _notificationService.notify.await_count == 2
        notifiedIds = {
            str(call.args[0]) for call in _notificationService.notify.call_args_list
        }
        assert str(TEST_PATIENT_USER_ID) in notifiedIds
        assert str(TEST_PHYSICIAN_ID) in notifiedIds

        auditLogger.record.assert_awaited_once()
        assert auditLogger.record.call_args[0][0] == "RESULT_RELEASED"
        assert auditLogger.record.call_args[1]["detailJson"]["release_method"] == "DIGITAL"


# ── Scenario 3: Result not APPROVED ───────────────────────────────────────────

class TestResultNotApproved:
    @pytest.mark.asyncio
    async def test_nonApprovedStatusRaises422(self):
        for badStatus in ("PENDING_CONFIRM", "RELEASED", "RETURNED_FOR_CORRECTION"):
            ar = _analysisResultRow(resultStatus=badStatus)
            db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar})
            _service, auditLogger, _ = _makeService(db)

            with pytest.raises(HTTPException) as excInfo:
                await _service.releaseResult(
                    TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
                )

            assert excInfo.value.status_code == 422
            assert excInfo.value.errorCode == "RESULT_NOT_APPROVED"
            auditLogger.record.assert_not_awaited()


# ── Scenario 4: Already released ──────────────────────────────────────────────

class TestAlreadyReleased:
    @pytest.mark.asyncio
    async def test_alreadyReleasedRaises422(self):
        ar = _analysisResultRow()
        db = _makeDb(
            {(AnalysisResult, TEST_RESULT_ID): ar}, existingReleaseId=TEST_RELEASE_ID
        )
        _service, auditLogger, _ = _makeService(db)

        with pytest.raises(HTTPException) as excInfo:
            await _service.releaseResult(
                TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
            )

        assert excInfo.value.status_code == 422
        assert excInfo.value.errorCode == "ALREADY_RELEASED"
        auditLogger.record.assert_not_awaited()


# ── Result not found ───────────────────────────────────────────────────────────

class TestResultNotFound:
    @pytest.mark.asyncio
    async def test_missingResultRaises404(self):
        db = _makeDb({})
        _service, auditLogger, _ = _makeService(db)

        with pytest.raises(HTTPException) as excInfo:
            await _service.releaseResult(
                TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
            )

        assert excInfo.value.status_code == 404
        assert excInfo.value.errorCode == "NOT_FOUND"


# ── Scenario 5: RBAC — SUPERVISOR forbidden on approved queue ─────────────────

def _mintToken(userId: uuid.UUID, role: str) -> str:
    from datetime import timedelta
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestRBAC:
    @pytest.mark.asyncio
    async def test_supervisorCannotAccessApprovedQueue(self, asyncClient):
        token = _mintToken(SUPERVISOR_ID, "SUPERVISOR")
        with patch(
            "src.core.rbac.isSessionActive", AsyncMock(return_value=True)
        ):
            response = await asyncClient.get(
                "/api/v1/results/approved",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 403
