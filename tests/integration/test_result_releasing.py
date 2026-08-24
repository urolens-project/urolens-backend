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
Service-layer tests (scenarios 1-4) mirror the pattern in tests/test_queue_service.py:
Supabase is replaced with a MagicMock per-test; no real DB or network required.
The RBAC test (scenario 5) uses httpx.AsyncClient + ASGITransport with a patched
is_session_active so auth middleware accepts the token without a live sessions table.
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


# ── Mock chain builder (mirrors test_queue_service.py) ────────────────────────

def _makeChain(returnData=None):
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.lt.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.execute = AsyncMock(return_value=MagicMock(data=returnData))
    return chain


def _makeService(db: MagicMock) -> tuple[ResultReleasingService, MagicMock, MagicMock]:
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


def _approvedResultRow(resultId=None, status="APPROVED"):
    rid = resultId or TEST_RESULT_ID
    return {
        "result_id": str(rid),
        "status": status,
        "patient_id": str(TEST_PATIENT_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _releaseRow(releaseId=None):
    return {
        "release_id": str(releaseId or TEST_RELEASE_ID),
        "result_id": str(TEST_RESULT_ID),
        "released_by": str(RECEPTIONIST_ID),
        "release_method": "PHYSICAL",
        "released_at": datetime.now(UTC).isoformat(),
    }


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
        callOrder: list[str] = []

        def tableSideEffect(tableName: str):
            callOrder.append(tableName)
            if tableName == "analysis_results" and callOrder.count("analysis_results") == 1:
                return _makeChain([_approvedResultRow()])
            elif tableName == "result_releases" and callOrder.count("result_releases") == 1:
                return _makeChain([])
            elif tableName == "result_releases" and callOrder.count("result_releases") == 2:
                return _makeChain([_releaseRow()])
            elif tableName == "analysis_results" and callOrder.count("analysis_results") == 2:
                return _makeChain([_approvedResultRow(status="RELEASED")])
            return _makeChain([])

        db = MagicMock()
        db.table.side_effect = tableSideEffect
        _service, auditLogger, _notificationService = _makeService(db)

        response = await _service.releaseResult(
            TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
        )

        assert response.resultId == TEST_RESULT_ID
        assert response.releaseMethod == "PHYSICAL"
        assert response.releaseId is not None

    @pytest.mark.asyncio
    async def test_physicalReleaseNoNotificationsSent(self):
        callOrder: list[str] = []

        def tableSideEffect(tableName: str):
            callOrder.append(tableName)
            if tableName == "analysis_results" and callOrder.count("analysis_results") == 1:
                return _makeChain([_approvedResultRow()])
            elif tableName == "result_releases" and callOrder.count("result_releases") == 1:
                return _makeChain([])
            elif tableName == "result_releases" and callOrder.count("result_releases") == 2:
                return _makeChain([_releaseRow()])
            elif tableName == "analysis_results":
                return _makeChain([_approvedResultRow(status="RELEASED")])
            return _makeChain([])

        db = MagicMock()
        db.table.side_effect = tableSideEffect
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
        callOrder: list[str] = []

        def tableSideEffect(tableName: str):
            callOrder.append(tableName)
            idx = callOrder.count(tableName)
            if tableName == "analysis_results" and idx == 1:
                return _makeChain([_approvedResultRow()])
            elif tableName == "result_releases" and idx == 1:
                return _makeChain([])
            elif tableName == "result_releases" and idx == 2:
                r = _releaseRow()
                r["release_method"] = "DIGITAL"
                return _makeChain([r])
            elif tableName == "analysis_results" and idx == 2:
                return _makeChain([_approvedResultRow(status="RELEASED")])
            elif tableName == "patients":
                return _makeChain([{"user_id": str(TEST_PATIENT_USER_ID)}])
            elif tableName == "specimens":
                return _makeChain([{"lab_request_id": str(TEST_LAB_REQUEST_ID)}])
            elif tableName == "lab_requests":
                return _makeChain([{"physician_id": str(TEST_PHYSICIAN_ID)}])
            return _makeChain([])

        db = MagicMock()
        db.table.side_effect = tableSideEffect
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
            db = MagicMock()
            db.table.return_value = _makeChain([_approvedResultRow(status=badStatus)])
            _service, auditLogger, _ = _makeService(db)

            with pytest.raises(HTTPException) as excInfo:
                await _service.releaseResult(
                    TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
                )

            assert excInfo.value.status_code == 422
            assert excInfo.value.detail["error"]["code"] == "RESULT_NOT_APPROVED"
            auditLogger.record.assert_not_awaited()


# ── Scenario 4: Already released ──────────────────────────────────────────────

class TestAlreadyReleased:
    @pytest.mark.asyncio
    async def test_alreadyReleasedRaises422(self):
        callOrder: list[str] = []

        def tableSideEffect(tableName: str):
            callOrder.append(tableName)
            if tableName == "analysis_results":
                return _makeChain([_approvedResultRow()])
            elif tableName == "result_releases":
                return _makeChain([{"release_id": str(TEST_RELEASE_ID)}])
            return _makeChain([])

        db = MagicMock()
        db.table.side_effect = tableSideEffect
        _service, auditLogger, _ = _makeService(db)

        with pytest.raises(HTTPException) as excInfo:
            await _service.releaseResult(
                TEST_RESULT_ID, "PHYSICAL", _currentUser(), _fakeRequest()
            )

        assert excInfo.value.status_code == 422
        assert excInfo.value.detail["error"]["code"] == "ALREADY_RELEASED"
        auditLogger.record.assert_not_awaited()


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
