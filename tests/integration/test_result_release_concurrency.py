"""Real-concurrency test — result_releasing_service.releaseResult (UROLENS-143).

`tests/integration/test_result_releasing.py` proves the race-closing
*mechanisms* (IntegrityError -> ALREADY_RELEASED, rowcount==0 -> same) with
deterministic, sequential mocks. That's necessary but not sufficient: it
never actually runs two requests at once, so it can't catch a bug where the
race is real but the app-level translation to ALREADY_RELEASED only happens
to work because the mocks are sequenced by the test itself.

This file runs two *real, concurrent HTTP requests* (via `asyncio.gather`
against the actual mounted route, per UROLENS-142's approach) against a
shared, `asyncio.Lock`-guarded fake session — one per request, both wired to
the same underlying fake "database" — and asserts on the **actual JSON
response bodies**, not on a raised exception object in isolation. That
distinction matters here specifically: UROLENS-143 item 6 (the double-nested
error envelope bug) shipped unnoticed precisely because prior tests only
checked the exception, never the real HTTP response — this test structurally
cannot repeat that mistake.

There is no real test database anywhere in this repo (Postgres-native enum
columns make a lightweight SQLite substitute impractical here, same
reasoning as UROLENS-142's concurrency test), so this fake is the closest
available proof that genuinely concurrent scheduling — not test-code
ordering — resolves to exactly one winner, and that the loser's error
reaches the client correctly.
"""
from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult

TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000150")
RECEPTIONIST_1 = uuid.UUID("00000000-0000-0000-0000-000000000151")
RECEPTIONIST_2 = uuid.UUID("00000000-0000-0000-0000-000000000152")


class _NestedCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, excType, exc, tb):
        return False


class _SharedFakeDb:
    """The one "database" both fake sessions race against."""

    def __init__(self, resultStatus: str = "APPROVED"):
        self.lock = asyncio.Lock()
        self.resultStatus = resultStatus
        self.activeReleaseFor: dict[uuid.UUID, uuid.UUID] = {}
        self.callLog: list[str] = []


class _RacingFakeSession:
    """One simulated request's `AsyncSession`, sharing `_SharedFakeDb` with
    the other racing request. Every method awaits `asyncio.sleep(0)` first —
    a genuine event-loop yield point, so `asyncio.gather` actually
    interleaves the two sessions' operations instead of one running to
    completion before the other starts.
    """

    def __init__(self, shared: _SharedFakeDb, label: str, analysisResult):
        self._shared = shared
        self._label = label
        self._analysisResult = analysisResult

    async def get(self, model, _id):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:get:{model.__name__}")
        if model is AnalysisResult:
            return self._analysisResult
        return None

    def add(self, _obj):
        pass

    async def flush(self, objs):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:flush")
        for obj in objs:
            async with self._shared.lock:
                if self._shared.activeReleaseFor.get(obj.resultId) is not None:
                    raise IntegrityError(
                        "INSERT INTO result_releases ...",
                        {},
                        Exception(
                            'duplicate key value violates unique constraint '
                            '"uq_result_releases_result_id"'
                        ),
                    )
                obj.releaseId = uuid.uuid4()
                self._shared.activeReleaseFor[obj.resultId] = obj.releaseId

    def begin_nested(self):
        return _NestedCtx()

    async def execute(self, stmt):
        await asyncio.sleep(0)
        self._shared.callLog.append(
            f"{self._label}:execute:{'select' if stmt.is_select else 'update' if stmt.is_update else 'other'}"
        )
        result = MagicMock()
        if stmt.is_select:
            result.scalar_one_or_none.return_value = None
            return result
        if stmt.is_update:
            params = stmt.compile().params
            async with self._shared.lock:
                if (
                    self._analysisResult.resultId == params["result_id_1"]
                    and self._shared.resultStatus == params["status_1"]
                ):
                    self._shared.resultStatus = params["status"]
                    result.rowcount = 1
                else:
                    result.rowcount = 0
            return result
        return result

    async def commit(self):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:commit")

    async def rollback(self):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:rollback")

    async def refresh(self, _obj):
        await asyncio.sleep(0)


def _mintToken(userId: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": "RECEPTIONIST",
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest.mark.asyncio
async def test_twoConcurrentReleasesOnSameResultOneWinsOneGetsAlreadyReleased():
    shared = _SharedFakeDb(resultStatus="APPROVED")

    analysisResult = MagicMock(spec=AnalysisResult)
    analysisResult.resultId = TEST_RESULT_ID
    analysisResult.status = "APPROVED"
    analysisResult.specimenId = None
    analysisResult.patientId = None

    labelCounter = itertools.count()

    async def _dbOverride():
        label = "A" if next(labelCounter) == 0 else "B"
        yield _RacingFakeSession(shared, label, analysisResult)

    app.dependency_overrides[getDb] = _dbOverride
    try:
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
            "src.api.result_releasing.AuditLogger"
        ) as mockAuditCls:
            mockAuditCls.return_value.record = AsyncMock()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                tokenA = _mintToken(RECEPTIONIST_1)
                tokenB = _mintToken(RECEPTIONIST_2)

                responseA, responseB = await asyncio.gather(
                    client.post(
                        f"/api/v1/results/{TEST_RESULT_ID}/release",
                        headers={"Authorization": f"Bearer {tokenA}"},
                        json={"releaseMethod": "PHYSICAL"},
                    ),
                    client.post(
                        f"/api/v1/results/{TEST_RESULT_ID}/release",
                        headers={"Authorization": f"Bearer {tokenB}"},
                        json={"releaseMethod": "PHYSICAL"},
                    ),
                )
    finally:
        app.dependency_overrides.pop(getDb, None)

    responses = [responseA, responseB]
    statusCodes = [r.status_code for r in responses]

    # Proof this was genuinely interleaved, not "A ran fully, then B ran":
    # both sides' pre-write calls appear before either side's flush.
    firstFlushIndex = next(i for i, e in enumerate(shared.callLog) if e.endswith(":flush"))
    assert any(e.startswith("A:") for e in shared.callLog[:firstFlushIndex])
    assert any(e.startswith("B:") for e in shared.callLog[:firstFlushIndex])

    successes = [r for r in responses if r.status_code == 201]
    failures = [r for r in responses if r.status_code != 201]

    assert len(successes) == 1, f"exactly one concurrent release must win, got status codes: {statusCodes}"
    assert len(failures) == 1, f"the other must fail cleanly, got status codes: {statusCodes}"

    failure = failures[0]
    assert failure.status_code == 422
    body = failure.json()
    assert body["error"]["code"] == "ALREADY_RELEASED", (
        f"the loser must get the real, distinct error code in the actual response "
        f"body, not a generic fallback: {body}"
    )

    # The DB itself ends up with exactly one release for the result, and the
    # result transitioned to RELEASED exactly once.
    assert len(shared.activeReleaseFor) == 1
    assert shared.resultStatus == "RELEASED"
