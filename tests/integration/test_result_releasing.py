"""
Integration tests — STORY-WEB-15: Result Releasing (T4.1)

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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from src.urolens.core.config import settings
from src.urolens.core.audit_logger import AuditLogger
from src.urolens.schemas.result_releasing import ReleaseResultRequest
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.result_releasing_service import ResultReleasingService

from main import app


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

def _make_chain(return_data=None):
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.lt.return_value = chain
    chain.order.return_value = chain
    chain.limit.return_value = chain
    chain.execute = AsyncMock(return_value=MagicMock(data=return_data))
    return chain


def _make_service(db: MagicMock) -> tuple[ResultReleasingService, MagicMock, MagicMock]:
    audit_logger = MagicMock()
    audit_logger.record = AsyncMock()
    notification_service = MagicMock()
    notification_service.notify = AsyncMock()
    service = ResultReleasingService(
        db=db,
        audit_logger=audit_logger,
        notification_service=notification_service,
    )
    return service, audit_logger, notification_service


def _approved_result_row(result_id=None, status="APPROVED"):
    rid = result_id or TEST_RESULT_ID
    return {
        "result_id": str(rid),
        "status": status,
        "patient_id": str(TEST_PATIENT_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _release_row(release_id=None):
    return {
        "release_id": str(release_id or TEST_RELEASE_ID),
        "result_id": str(TEST_RESULT_ID),
        "released_by": str(RECEPTIONIST_ID),
        "release_method": "PHYSICAL",
        "released_at": datetime.now(timezone.utc).isoformat(),
    }


def _current_user(user_id=None, role="RECEPTIONIST"):
    return {"user_id": str(user_id or RECEPTIONIST_ID), "role": role}


def _fake_request():
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


# ── Scenario 1: PHYSICAL release ─────────────────────────────────────────────

class TestPhysicalRelease:
    @pytest.mark.asyncio
    async def test_physical_release_returns_response(self):
        call_order: list[str] = []

        def table_side_effect(table_name: str):
            call_order.append(table_name)
            if table_name == "analysis_results" and call_order.count("analysis_results") == 1:
                return _make_chain([_approved_result_row()])
            elif table_name == "result_releases" and call_order.count("result_releases") == 1:
                return _make_chain([])
            elif table_name == "result_releases" and call_order.count("result_releases") == 2:
                return _make_chain([_release_row()])
            elif table_name == "analysis_results" and call_order.count("analysis_results") == 2:
                return _make_chain([_approved_result_row(status="RELEASED")])
            return _make_chain([])

        db = MagicMock()
        db.table.side_effect = table_side_effect
        service, audit_logger, notification_service = _make_service(db)

        response = await service.release_result(
            TEST_RESULT_ID, "PHYSICAL", _current_user(), _fake_request()
        )

        assert response.result_id == TEST_RESULT_ID
        assert response.release_method == "PHYSICAL"
        assert response.release_id is not None

    @pytest.mark.asyncio
    async def test_physical_release_no_notifications_sent(self):
        call_order: list[str] = []

        def table_side_effect(table_name: str):
            call_order.append(table_name)
            if table_name == "analysis_results" and call_order.count("analysis_results") == 1:
                return _make_chain([_approved_result_row()])
            elif table_name == "result_releases" and call_order.count("result_releases") == 1:
                return _make_chain([])
            elif table_name == "result_releases" and call_order.count("result_releases") == 2:
                return _make_chain([_release_row()])
            elif table_name == "analysis_results":
                return _make_chain([_approved_result_row(status="RELEASED")])
            return _make_chain([])

        db = MagicMock()
        db.table.side_effect = table_side_effect
        service, audit_logger, notification_service = _make_service(db)

        await service.release_result(
            TEST_RESULT_ID, "PHYSICAL", _current_user(), _fake_request()
        )

        notification_service.notify.assert_not_awaited()
        audit_logger.record.assert_awaited_once()
        args = audit_logger.record.call_args
        assert args[0][0] == "RESULT_RELEASED"
        assert args[1]["entity_type"] == "result_release"
        assert args[1]["detail_json"]["release_method"] == "PHYSICAL"


# ── Scenario 2: DIGITAL release ───────────────────────────────────────────────

class TestDigitalRelease:
    @pytest.mark.asyncio
    async def test_digital_release_notifies_patient_and_physician(self):
        call_order: list[str] = []

        def table_side_effect(table_name: str):
            call_order.append(table_name)
            idx = call_order.count(table_name)
            if table_name == "analysis_results" and idx == 1:
                return _make_chain([_approved_result_row()])
            elif table_name == "result_releases" and idx == 1:
                return _make_chain([])
            elif table_name == "result_releases" and idx == 2:
                r = _release_row()
                r["release_method"] = "DIGITAL"
                return _make_chain([r])
            elif table_name == "analysis_results" and idx == 2:
                return _make_chain([_approved_result_row(status="RELEASED")])
            elif table_name == "patients":
                return _make_chain([{"user_id": str(TEST_PATIENT_USER_ID)}])
            elif table_name == "specimens":
                return _make_chain([{"lab_request_id": str(TEST_LAB_REQUEST_ID)}])
            elif table_name == "lab_requests":
                return _make_chain([{"physician_id": str(TEST_PHYSICIAN_ID)}])
            return _make_chain([])

        db = MagicMock()
        db.table.side_effect = table_side_effect
        service, audit_logger, notification_service = _make_service(db)

        await service.release_result(
            TEST_RESULT_ID, "DIGITAL", _current_user(), _fake_request()
        )

        assert notification_service.notify.await_count == 2
        notified_ids = {
            str(call.args[0]) for call in notification_service.notify.call_args_list
        }
        assert str(TEST_PATIENT_USER_ID) in notified_ids
        assert str(TEST_PHYSICIAN_ID) in notified_ids

        audit_logger.record.assert_awaited_once()
        assert audit_logger.record.call_args[0][0] == "RESULT_RELEASED"
        assert audit_logger.record.call_args[1]["detail_json"]["release_method"] == "DIGITAL"


# ── Scenario 3: Result not APPROVED ───────────────────────────────────────────

class TestResultNotApproved:
    @pytest.mark.asyncio
    async def test_non_approved_status_raises_422(self):
        for bad_status in ("PENDING_CONFIRM", "RELEASED", "RETURNED_FOR_CORRECTION"):
            db = MagicMock()
            db.table.return_value = _make_chain([_approved_result_row(status=bad_status)])
            service, audit_logger, _ = _make_service(db)

            with pytest.raises(HTTPException) as exc_info:
                await service.release_result(
                    TEST_RESULT_ID, "PHYSICAL", _current_user(), _fake_request()
                )

            assert exc_info.value.status_code == 422
            assert exc_info.value.detail["error"]["code"] == "RESULT_NOT_APPROVED"
            audit_logger.record.assert_not_awaited()


# ── Scenario 4: Already released ──────────────────────────────────────────────

class TestAlreadyReleased:
    @pytest.mark.asyncio
    async def test_already_released_raises_422(self):
        call_order: list[str] = []

        def table_side_effect(table_name: str):
            call_order.append(table_name)
            if table_name == "analysis_results":
                return _make_chain([_approved_result_row()])
            elif table_name == "result_releases":
                return _make_chain([{"release_id": str(TEST_RELEASE_ID)}])
            return _make_chain([])

        db = MagicMock()
        db.table.side_effect = table_side_effect
        service, audit_logger, _ = _make_service(db)

        with pytest.raises(HTTPException) as exc_info:
            await service.release_result(
                TEST_RESULT_ID, "PHYSICAL", _current_user(), _fake_request()
            )

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "ALREADY_RELEASED"
        audit_logger.record.assert_not_awaited()


# ── Scenario 5: RBAC — SUPERVISOR forbidden on approved queue ─────────────────

def _mint_token(user_id: uuid.UUID, role: str) -> str:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": str(user_id),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=settings.jwt_algorithm)


@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestRBAC:
    @pytest.mark.asyncio
    async def test_supervisor_cannot_access_approved_queue(self, async_client):
        token = _mint_token(SUPERVISOR_ID, "SUPERVISOR")
        with patch(
            "src.urolens.core.rbac.is_session_active", AsyncMock(return_value=True)
        ):
            response = await async_client.get(
                "/api/v1/results/approved",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 403
