"""Integration tests — STORY-WEB-15 / UROLENS-143: Result Releasing.

Covers
------
1. PHYSICAL release — 201, result_releases row created, status = RELEASED,
   no notifications, audit_log has RESULT_RELEASED
2. DIGITAL release — same as above plus notification rows for patient and physician
3. Not APPROVED — result in wrong status -> 422, code = RESULT_NOT_APPROVED
4. Not found -> 404, code = RESULT_NOT_FOUND
5. Already released (pre-check, constraint violation, and rowcount==0 race
   paths) -> 422, code = ALREADY_RELEASED
6. RBAC — SUPERVISOR JWT on GET /api/v1/results/approved -> 403
7. GET /api/v1/results/approved no longer returns patientName; returns patientUid

Every assertion in this file is against the real HTTP response (status code +
JSON body), not against the raised exception object in isolation — that gap
(asserting only `excInfo.value.detail[...]`) is exactly what let the
double-nested-envelope bug (UROLENS-143 item 6) ship unnoticed previously.

Architecture
------------
Drives the real mounted route through `httpx.AsyncClient` with `getDb`
overridden to a mocked `AsyncSession` (mirrors
`tests/integration/test_labeling_rbac.py`'s pattern). `db.execute`'s mock
dispatches on the statement's `is_select`/`is_update` flag so the same
builder covers the pre-check SELECT and the conditional UPDATE the race fix
introduced. `AuditLogger` is patched to avoid a real Supabase network
attempt (same convention as every other integration test in this repo).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.models.result_release import ResultRelease
from src.models.specimen import Specimen

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


def _patientRow(patientId=None, userId=None, patientUid="PAT-000123"):
    p = MagicMock(spec=Patient)
    p.patientId = patientId or TEST_PATIENT_ID
    p.userId = userId if userId is not None else TEST_PATIENT_USER_ID
    p.patientUid = patientUid
    return p


def _labRequestRow(labRequestId=None, physicianId=None):
    lr = MagicMock(spec=LabRequest)
    lr.labRequestId = labRequestId or TEST_LAB_REQUEST_ID
    lr.physicianId = physicianId if physicianId is not None else TEST_PHYSICIAN_ID
    return lr


class _NestedCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, excType, exc, tb):
        return False


def _makeDb(
    getMap: dict,
    existingReleaseId=None,
    updateRowcount: int = 1,
    flushRaises: Exception | None = None,
) -> AsyncMock:
    """`db.get(Model, id)` is backed by `getMap`. `db.execute()` dispatches
    on the statement shape: the pre-check `SELECT` (backed by
    `existingReleaseId`) or the conditional `UPDATE` (backed by
    `updateRowcount`) — anything else (e.g. a `NotificationService` insert)
    gets an unused, harmless `MagicMock()` back. `db.flush()` assigns
    `releaseId`/`releasedAt` the way a real flush against Postgres
    (server-generated defaults) would, or raises `flushRaises` to simulate
    the unique-constraint-violation race path.
    """
    db = AsyncMock()

    async def _get(model, id_):
        return getMap.get((model, id_))

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()

    async def _execute(stmt, *args, **kwargs):
        if getattr(stmt, "is_select", False):
            result = MagicMock()
            result.scalar_one_or_none.return_value = existingReleaseId
            return result
        if getattr(stmt, "is_update", False):
            result = MagicMock()
            result.rowcount = updateRowcount
            return result
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)

    async def _flush(objs):
        if flushRaises is not None:
            raise flushRaises
        for obj in objs:
            if isinstance(obj, ResultRelease):
                obj.releaseId = TEST_RELEASE_ID
                obj.releasedAt = datetime.now(UTC)

    db.flush = AsyncMock(side_effect=_flush)
    db.begin_nested = MagicMock(return_value=_NestedCtx())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _overrideDb(db):
    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override


def _clearDbOverride():
    app.dependency_overrides.pop(getDb, None)


def _mintToken(userId: uuid.UUID, role: str) -> str:
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


def _receptionistHeaders() -> dict:
    return {"Authorization": f"Bearer {_mintToken(RECEPTIONIST_ID, 'RECEPTIONIST')}"}


# ── Scenario 1: PHYSICAL release ─────────────────────────────────────────────

class TestPhysicalRelease:
    @pytest.mark.asyncio
    async def test_physicalReleaseReturns201(self, asyncClient):
        ar = _analysisResultRow()
        spec = _specimenRow()
        db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar, (Specimen, TEST_SPECIMEN_ID): spec})
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 201
        body = response.json()
        assert body["resultId"] == str(TEST_RESULT_ID)
        assert body["releaseMethod"] == "PHYSICAL"
        assert body["releaseId"] == str(TEST_RELEASE_ID)
        assert spec.status == "COMPLETED"
        db.commit.assert_awaited_once()
        mockAuditCls.return_value.record.assert_awaited_once()
        auditArgs = mockAuditCls.return_value.record.call_args
        assert auditArgs[0][0] == "RESULT_RELEASED"
        assert auditArgs[1]["entityType"] == "result_release"
        assert auditArgs[1]["detailJson"]["release_method"] == "PHYSICAL"


# ── Scenario 2: DIGITAL release ───────────────────────────────────────────────

class TestDigitalRelease:
    @pytest.mark.asyncio
    async def test_digitalReleaseNotifiesPatientAndPhysician(self, asyncClient):
        ar = _analysisResultRow()
        spec = _specimenRow()
        patient = _patientRow()
        labRequest = _labRequestRow()
        db = _makeDb(
            {
                (AnalysisResult, TEST_RESULT_ID): ar,
                (Specimen, TEST_SPECIMEN_ID): spec,
                (Patient, TEST_PATIENT_ID): patient,
                (LabRequest, TEST_LAB_REQUEST_ID): labRequest,
            }
        )
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls, patch(
                "src.services.notification_service.NotificationService.notify",
                new_callable=AsyncMock,
            ) as mockNotify:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "DIGITAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 201
        assert response.json()["releaseMethod"] == "DIGITAL"
        assert mockNotify.await_count == 2
        notifiedIds = {str(call.args[0]) for call in mockNotify.call_args_list}
        assert str(TEST_PATIENT_USER_ID) in notifiedIds
        assert str(TEST_PHYSICIAN_ID) in notifiedIds


# ── Scenario 3: not APPROVED ─────────────────────────────────────────────────

class TestResultNotApproved:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("badStatus", ["PENDING_CONFIRM", "RELEASED", "RETURNED_FOR_CORRECTION"])
    async def test_nonApprovedStatusReturns422(self, asyncClient, badStatus):
        ar = _analysisResultRow(resultStatus=badStatus)
        db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar})
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RESULT_NOT_APPROVED"
        mockAuditCls.return_value.record.assert_not_awaited()
        db.commit.assert_not_awaited()


# ── Scenario 4: not found ─────────────────────────────────────────────────────

class TestResultNotFound:
    @pytest.mark.asyncio
    async def test_missingResultReturns404(self, asyncClient):
        db = _makeDb({})
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESULT_NOT_FOUND"


# ── Scenario 5: already released — all three converging paths ───────────────

class TestAlreadyReleased:
    @pytest.mark.asyncio
    async def test_alreadyReleasedViaPreCheckReturns422(self, asyncClient):
        ar = _analysisResultRow()
        db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar}, existingReleaseId=TEST_RELEASE_ID)
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ALREADY_RELEASED"
        mockAuditCls.return_value.record.assert_not_awaited()
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_alreadyReleasedViaConstraintViolationReturns422(self, asyncClient):
        """The pre-check passes (no release visible yet), but the insert
        itself violates the unique constraint — the race path.
        """
        from sqlalchemy.exc import IntegrityError

        ar = _analysisResultRow()
        integrityError = IntegrityError(
            "INSERT INTO result_releases ...",
            {},
            Exception(
                'duplicate key value violates unique constraint "uq_result_releases_result_id"'
            ),
        )
        db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar}, flushRaises=integrityError)
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ALREADY_RELEASED"
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alreadyReleasedViaConditionalUpdateRowcountZeroReturns422(self, asyncClient):
        """The insert succeeds but the conditional UPDATE affects 0 rows
        (the result's status flipped between the first read and this
        UPDATE) — same ALREADY_RELEASED, and rolled back rather than left
        with an orphaned release row.
        """
        ar = _analysisResultRow()
        db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar}, updateRowcount=0)
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
                "src.api.result_releasing.AuditLogger"
            ) as mockAuditCls:
                mockAuditCls.return_value.record = AsyncMock()
                response = await asyncClient.post(
                    f"/api/v1/results/{TEST_RESULT_ID}/release",
                    headers=_receptionistHeaders(),
                    json={"releaseMethod": "PHYSICAL"},
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ALREADY_RELEASED"
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()


# ── The three codes are genuinely distinguishable from each other ──────────

@pytest.mark.asyncio
async def test_theThreeFailureCodesAreAllDistinct(asyncClient):
    codes = set()

    db = _makeDb({})
    _overrideDb(db)
    try:
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            r = await asyncClient.post(
                f"/api/v1/results/{TEST_RESULT_ID}/release",
                headers=_receptionistHeaders(),
                json={"releaseMethod": "PHYSICAL"},
            )
    finally:
        _clearDbOverride()
    assert r.status_code == 404
    codes.add(r.json()["error"]["code"])

    ar = _analysisResultRow(resultStatus="PENDING_CONFIRM")
    db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar})
    _overrideDb(db)
    try:
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
            "src.api.result_releasing.AuditLogger"
        ) as mockAuditCls:
            mockAuditCls.return_value.record = AsyncMock()
            r = await asyncClient.post(
                f"/api/v1/results/{TEST_RESULT_ID}/release",
                headers=_receptionistHeaders(),
                json={"releaseMethod": "PHYSICAL"},
            )
    finally:
        _clearDbOverride()
    assert r.status_code == 422
    codes.add(r.json()["error"]["code"])

    ar2 = _analysisResultRow()
    db = _makeDb({(AnalysisResult, TEST_RESULT_ID): ar2}, existingReleaseId=TEST_RELEASE_ID)
    _overrideDb(db)
    try:
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
            "src.api.result_releasing.AuditLogger"
        ) as mockAuditCls:
            mockAuditCls.return_value.record = AsyncMock()
            r = await asyncClient.post(
                f"/api/v1/results/{TEST_RESULT_ID}/release",
                headers=_receptionistHeaders(),
                json={"releaseMethod": "PHYSICAL"},
            )
    finally:
        _clearDbOverride()
    assert r.status_code == 422
    codes.add(r.json()["error"]["code"])

    assert codes == {"RESULT_NOT_FOUND", "RESULT_NOT_APPROVED", "ALREADY_RELEASED"}, (
        f"expected three distinct codes, got: {codes}"
    )


# ── Scenario 6: RBAC — SUPERVISOR forbidden ──────────────────────────────────

class TestRBAC:
    @pytest.mark.asyncio
    async def test_supervisorCannotAccessApprovedQueue(self, asyncClient):
        token = _mintToken(SUPERVISOR_ID, "SUPERVISOR")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/results/approved",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    @pytest.mark.asyncio
    async def test_supervisorCannotRelease(self, asyncClient):
        token = _mintToken(SUPERVISOR_ID, "SUPERVISOR")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/results/{TEST_RESULT_ID}/release",
                headers={"Authorization": f"Bearer {token}"},
                json={"releaseMethod": "PHYSICAL"},
            )
        assert response.status_code == 403


# ── Scenario 7: list endpoint returns patientUid, not patientName ──────────

class TestApprovedResultsListShape:
    @pytest.mark.asyncio
    async def test_listReturnsPatientUidNotPatientName(self, asyncClient):
        ar = _analysisResultRow()
        spec = _specimenRow()
        patient = _patientRow(patientUid="PAT-000456")

        listResult = MagicMock()
        listResult.scalars.return_value.all.return_value = [ar]

        db = AsyncMock()
        db.execute = AsyncMock(return_value=listResult)

        async def _get(model, id_):
            return {
                (Patient, TEST_PATIENT_ID): patient,
                (Specimen, TEST_SPECIMEN_ID): spec,
            }.get((model, id_))

        db.get = AsyncMock(side_effect=_get)
        _overrideDb(db)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
                response = await asyncClient.get(
                    "/api/v1/results/approved",
                    headers=_receptionistHeaders(),
                )
        finally:
            _clearDbOverride()

        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 1
        item = body["data"][0]
        assert "patientName" not in item
        assert item["patientUid"] == "PAT-000456"
        assert item["sampleUid"] == "SAMP-001"
        assert item["testType"] == "URINALYSIS"
