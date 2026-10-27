"""Real-concurrency test — approve/return/escalate a result (UROLENS-151).

`tests/test_result_review_service.py` proves the race-closing *mechanism*
(conditional UPDATE, rowcount==0 -> INVALID_RESULT_STATUS) with deterministic,
sequential mocks. That's necessary but not sufficient: it never actually runs
two requests at once, so it can't catch a bug where the app-level translation
to INVALID_RESULT_STATUS only happens to work because the mocks are sequenced
by the test itself.

This file runs three *real, concurrent HTTP requests* (via `asyncio.gather`
against the actual mounted routes, per UROLENS-142/143's approach) — one
approve, one return, one escalate, all targeting the same result — against a
shared, `asyncio.Lock`-guarded fake session, and asserts on the **actual JSON
response bodies**. The scenario is deliberately cross-action (not two calls to
the same action) because that's the case a per-action-table unique constraint
would NOT catch: only the conditional UPDATE against the shared
`analysis_results.status` protects every combination of the three actions
against each other.

There is no real test database anywhere in this repo (Postgres-native enum
columns make a lightweight SQLite substitute impractical here, same reasoning
as UROLENS-142/143's concurrency tests), so this fake is the closest available
proof that genuinely concurrent scheduling — not test-code ordering —
resolves to exactly one winner, and that the losers' errors reach the client
correctly.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from src.api.results import getNotifService
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.specimen import Specimen

TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000151")
TEST_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000152")
SUPERVISOR_1 = uuid.UUID("00000000-0000-0000-0000-000000000153")
SUPERVISOR_2 = uuid.UUID("00000000-0000-0000-0000-000000000154")
SUPERVISOR_3 = uuid.UUID("00000000-0000-0000-0000-000000000155")


class _SharedFakeDb:
    """The one "database" all three racing requests contend for."""

    def __init__(self, parties: int = 3) -> None:
        self.lock = asyncio.Lock()
        # Forces genuinely simultaneous arrival at the first DB call,
        # regardless of how much real (variable-latency) work — JWT decode,
        # RBAC, routing — each request does before reaching it. Without this,
        # three real HTTP requests are not guaranteed to interleave (unlike a
        # bare `asyncio.sleep(0)` yield, which only helps once all three are
        # already scheduled) — observed directly: the plain-yield version of
        # this test flaked roughly 2 times in 5.
        self.barrier = asyncio.Barrier(parties)
        self.resultStatus: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL
        self.callLog: list[str] = []


class _RacingFakeSession:
    """One simulated request's `AsyncSession`, sharing `_SharedFakeDb` with
    the other two racing requests. The first call blocks at the shared
    barrier until all three have arrived, guaranteeing true simultaneous
    start; every method after that awaits `asyncio.sleep(0)` too, so
    `asyncio.gather` keeps interleaving the three sessions' operations
    instead of one running to completion before the next starts.
    """

    def __init__(self, shared: _SharedFakeDb, label: str, analysisResult, specimen) -> None:
        self._shared = shared
        self._label = label
        self._analysisResult = analysisResult
        self._specimen = specimen
        self._waitedAtBarrier = False

    async def _yield(self) -> None:
        if not self._waitedAtBarrier:
            self._waitedAtBarrier = True
            await self._shared.barrier.wait()
        await asyncio.sleep(0)

    async def get(self, model, _id):
        await self._yield()
        self._shared.callLog.append(f"{self._label}:get:{model.__name__}")
        if model is AnalysisResult:
            return self._analysisResult
        if model is Specimen:
            return self._specimen
        return None

    def add(self, obj):
        self._shared.callLog.append(f"{self._label}:add:{type(obj).__name__}")

    async def execute(self, stmt):
        await self._yield()
        self._shared.callLog.append(
            f"{self._label}:execute:{'update' if stmt.is_update else 'other'}"
        )
        result = MagicMock()
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
        await self._yield()
        self._shared.callLog.append(f"{self._label}:commit")

    async def rollback(self):
        await self._yield()
        self._shared.callLog.append(f"{self._label}:rollback")

    async def refresh(self, _obj):
        await asyncio.sleep(0)


def _mintToken(userId: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": "SUPERVISOR",
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest.mark.asyncio
async def test_approveReturnEscalateRacingOnSameResultExactlyOneWins():
    shared = _SharedFakeDb()

    analysisResult = MagicMock(spec=AnalysisResult)
    analysisResult.resultId = TEST_RESULT_ID
    analysisResult.specimenId = TEST_SPECIMEN_ID
    analysisResult.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL

    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = TEST_SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.completedAt = None

    labels = iter(["A", "B", "C"])

    async def _dbOverride():
        label = next(labels)
        yield _RacingFakeSession(shared, label, analysisResult, specimen)

    app.dependency_overrides[getDb] = _dbOverride
    # The return, if it wins, notifies the MedTech; not what this test is about.
    app.dependency_overrides[getNotifService] = lambda: MagicMock(notifyMedtechResultReturned=AsyncMock())
    try:
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                tokenApprove = _mintToken(SUPERVISOR_1)
                tokenReturn = _mintToken(SUPERVISOR_2)
                tokenEscalate = _mintToken(SUPERVISOR_3)

                approveResponse, returnResponse, escalateResponse = await asyncio.gather(
                    client.post(
                        f"/api/v1/results/{TEST_RESULT_ID}/approve",
                        headers={"Authorization": f"Bearer {tokenApprove}"},
                        json={"notes": "Looks good"},
                    ),
                    client.post(
                        f"/api/v1/results/{TEST_RESULT_ID}/return",
                        headers={"Authorization": f"Bearer {tokenReturn}"},
                        json={"reason": "Blurry image"},
                    ),
                    client.post(
                        f"/api/v1/results/{TEST_RESULT_ID}/escalate",
                        headers={"Authorization": f"Bearer {tokenEscalate}"},
                        json={"escalationPath": "MARK_CRITICAL", "escalationNote": "Urgent"},
                    ),
                )
    finally:
        app.dependency_overrides.pop(getDb, None)
        app.dependency_overrides.pop(getNotifService, None)

    responses = {
        "approve": approveResponse,
        "return": returnResponse,
        "escalate": escalateResponse,
    }
    statusCodes = {name: r.status_code for name, r in responses.items()}

    # Proof this was genuinely interleaved, not "one ran fully, then the
    # next": all three sides' pre-write calls appear before the first commit.
    firstCommitIndex = next(i for i, e in enumerate(shared.callLog) if e.endswith(":commit"))
    assert any(e.startswith("A:") for e in shared.callLog[:firstCommitIndex])
    assert any(e.startswith("B:") for e in shared.callLog[:firstCommitIndex])
    assert any(e.startswith("C:") for e in shared.callLog[:firstCommitIndex])

    successes = [name for name, r in responses.items() if r.status_code == 200]
    failures = [name for name, r in responses.items() if r.status_code != 200]

    assert len(successes) == 1, f"exactly one of approve/return/escalate must win, got: {statusCodes}"
    assert len(failures) == 2, f"the other two must fail cleanly, got: {statusCodes}"

    for name in failures:
        failure = responses[name]
        assert failure.status_code == 409, f"{name}: expected 409, got {failure.status_code}: {failure.text}"
        body = failure.json()
        assert body["error"]["code"] == "INVALID_RESULT_STATUS", (
            f"{name}: the loser must get the real, distinct error code in the actual "
            f"response body, not a generic fallback: {body}"
        )

    # The shared result transitioned exactly once, to whichever action won.
    winningStatus = {
        "approve": ResultStatus.APPROVED,
        "return": ResultStatus.RETURNED_FOR_CORRECTION,
        "escalate": ResultStatus.CRITICAL_ESCALATED,
    }[successes[0]]
    assert shared.resultStatus == winningStatus
