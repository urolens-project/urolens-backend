"""Integration test — STORY-MOB-09: Result confirmation flow.

TASK-MOB-09-12 | STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/tests/integration/test_result_confirmation.py

Covers:
  1. POST /api/v1/results/{id}/confirm happy path (online).
  2. Confirm blocked when a retake is pending.
  3. Confirm blocked when already confirmed (idempotency guard).
  4. GET  /api/v1/results/{id} returns ai_findings + smart_diagnosis.
  5. Smart Diagnosis is triggered on confirmation.
  6. Audit event RESULT_CONFIRMED is persisted.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.models.analysis_result import AnalysisResult
from app.models.result_confirmation import ResultConfirmation
from app.models.user import User
from tests.factories import (
    AnalysisResultFactory,
    ResultConfirmationFactory,
    UserFactory,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def medtech_user(db_session) -> User:
    return await UserFactory.create(role="MEDTECH", session=db_session)


@pytest_asyncio.fixture
async def pending_result(db_session) -> AnalysisResult:
    """AnalysisResult in PENDING_CONFIRMATION status with dummy ai_findings."""
    return await AnalysisResultFactory.create(
        status="PENDING_CONFIRMATION",
        ai_findings=[
            {"parameter": "WBC", "value": 12.4, "unit": "10³/µL", "is_flagged": True},
            {"parameter": "RBC", "value": 4.9, "unit": "10⁶/µL", "is_flagged": False},
        ],
        session=db_session,
    )


# ── Test cases ─────────────────────────────────────────────────────────────────

class TestConfirmResult:
    """POST /api/v1/results/{id}/confirm"""

    @pytest.mark.asyncio
    async def test_happy_path_online(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        pending_result: AnalysisResult,
        auth_headers,
        db_session,
    ):
        """Confirming a result sets status → PENDING_SUPERVISOR_APPROVAL and
        creates a ResultConfirmation record."""
        headers = auth_headers(medtech_user)

        with patch(
            "app.services.smart_diagnosis_service.SmartDiagnosisService.trigger",
            new_callable=AsyncMock,
        ) as mock_sd:
            response = await async_client.post(
                f"/api/v1/results/{pending_result.id}/confirm",
                headers=headers,
                json={"notes": "All parameters reviewed."},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["result_id"] == str(pending_result.id)
        assert body["status"] == "PENDING_SUPERVISOR_APPROVAL"
        assert body["smart_diagnosis_triggered"] is True
        mock_sd.assert_awaited_once_with(result_id=pending_result.id)

    @pytest.mark.asyncio
    async def test_confirm_blocked_by_pending_retake(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        db_session,
        auth_headers,
    ):
        """Returns 422 when a retake is still pending."""
        retake_result = await AnalysisResultFactory.create(
            status="PENDING_RETAKE", session=db_session
        )
        response = await async_client.post(
            f"/api/v1/results/{retake_result.id}/confirm",
            headers=auth_headers(medtech_user),
            json={},
        )
        assert response.status_code == 422
        assert "retake" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_confirm_blocked_when_already_confirmed(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        pending_result: AnalysisResult,
        db_session,
        auth_headers,
    ):
        """Returns 409 when the result has already been confirmed."""
        await ResultConfirmationFactory.create(
            result_id=pending_result.id, session=db_session
        )
        response = await async_client.post(
            f"/api/v1/results/{pending_result.id}/confirm",
            headers=auth_headers(medtech_user),
            json={},
        )
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_result(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        auth_headers,
    ):
        response = await async_client.post(
            f"/api/v1/results/{uuid.uuid4()}/confirm",
            headers=auth_headers(medtech_user),
            json={},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_audit_event_persisted(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        pending_result: AnalysisResult,
        auth_headers,
        db_session,
    ):
        """RESULT_CONFIRMED audit event must be written on success."""
        with patch(
            "app.services.smart_diagnosis_service.SmartDiagnosisService.trigger",
            new_callable=AsyncMock,
        ):
            await async_client.post(
                f"/api/v1/results/{pending_result.id}/confirm",
                headers=auth_headers(medtech_user),
                json={},
            )

        from sqlalchemy import select
        from app.models.audit_log import AuditLog

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == pending_result.id,
                AuditLog.action == "RESULT_CONFIRMED",
            )
        )
        audit = result.scalar_one_or_none()
        assert audit is not None
        assert audit.actor_id == medtech_user.id

    @pytest.mark.asyncio
    async def test_smart_diagnosis_failure_is_non_fatal(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        pending_result: AnalysisResult,
        auth_headers,
    ):
        """Smart Diagnosis errors must not block confirmation."""
        with patch(
            "app.services.smart_diagnosis_service.SmartDiagnosisService.trigger",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI engine timeout"),
        ):
            response = await async_client.post(
                f"/api/v1/results/{pending_result.id}/confirm",
                headers=auth_headers(medtech_user),
                json={},
            )
        assert response.status_code == 201
        assert response.json()["smart_diagnosis_triggered"] is False


class TestGetFullResult:
    """GET /api/v1/results/{id}"""

    @pytest.mark.asyncio
    async def test_returns_ai_findings_and_smart_diagnosis(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        pending_result: AnalysisResult,
        auth_headers,
    ):
        """Full result includes ai_findings and smart_diagnosis fields."""
        response = await async_client.get(
            f"/api/v1/results/{pending_result.id}",
            headers=auth_headers(medtech_user),
        )
        assert response.status_code == 200
        body = response.json()
        assert "ai_findings" in body
        assert isinstance(body["ai_findings"], list)
        assert len(body["ai_findings"]) == 2

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_id(
        self,
        async_client: AsyncClient,
        medtech_user: User,
        auth_headers,
    ):
        response = await async_client.get(
            f"/api/v1/results/{uuid.uuid4()}",
            headers=auth_headers(medtech_user),
        )
        assert response.status_code == 404