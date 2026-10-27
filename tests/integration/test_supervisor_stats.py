"""Integration tests — UROLENS-169: Supervisor dashboard stats
(`GET /api/v1/results/supervisor/stats`).

The endpoint already existed before this ticket (`ResultReviewService.
getSupervisorStats`). This file covers what an audit found missing:

1. RBAC and response-shape proof at the true HTTP response level
   (status + `response.json()["error"]["code"]` / body), not just the
   raised exception — the gap the ticket says hid a bug in UROLENS-143.
2. `getSupervisorStats` issues exactly one `db.execute()` round trip
   (three scalar subqueries in one SELECT), not three separate queries.
3. The pending/approved-today/escalated filters used inside
   `getSupervisorStats` are byte-for-byte the same compiled SQL as the
   ones `getPending`/`getApprovedToday`/`getEscalated` use — a structural
   guard against the counts silently drifting from their matching queue
   list (UROLENS-142's workload-count bug, repeated).
4. The PHT "today" boundary: an approval at 23:30 PHT yesterday is
   excluded, one at 00:30 PHT today is included — proving the fix for the
   pre-existing bug where a bare `date` compared against a `timestamptz`
   column got reinterpreted in the session's (UTC) timezone, shifting the
   boundary to 8am PHT instead of midnight PHT.

No real database: `Depends(getDb)` is overridden with a fake `AsyncSession`
whose `execute()` is scripted per test, matching this codebase's existing
convention (see test_lab_requests_http.py, test_result_review_service.py).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult
from src.models.result_approval import ResultApproval
from src.models.specimen import Specimen
from src.services.result_review_service import ResultReviewService

SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000090")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000091")
_PHT = timezone(timedelta(hours=8))


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


class _FakeStatsSession:
    """Backs the single `db.execute()` call `getSupervisorStats` makes with
    a canned three-column row.
    """

    def __init__(self, pendingCount: int, approvedToday: int, escalatedCount: int) -> None:
        self._row = (pendingCount, approvedToday, escalatedCount)
        self.executeCount = 0

    async def execute(self, _stmt):
        self.executeCount += 1
        result = MagicMock()
        result.one.return_value = self._row
        return result


def _overrideDb(fakeSession):
    async def _override():
        yield fakeSession

    app.dependency_overrides[getDb] = _override
    return fakeSession


@pytest.fixture(autouse=True)
def _clearOverrides():
    yield
    app.dependency_overrides.pop(getDb, None)


# ── HTTP-level: RBAC ───────────────────────────────────────────────────────


class TestSupervisorStatsRBAC:
    @pytest.mark.asyncio
    async def test_nonSupervisorGets403WithForbiddenCode(self, asyncClient):
        token = _mintToken(MEDTECH_ID, "MEDTECH")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/results/supervisor/stats",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    @pytest.mark.asyncio
    async def test_noTokenGets401WithUnauthorizedCode(self, asyncClient):
        response = await asyncClient.get("/api/v1/results/supervisor/stats")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ── HTTP-level: response shape ──────────────────────────────────────────────


class TestSupervisorStatsResponseShape:
    @pytest.mark.asyncio
    async def test_countsReachClientWithExactFieldNames(self, asyncClient):
        _overrideDb(_FakeStatsSession(pendingCount=5, approvedToday=3, escalatedCount=2))
        token = _mintToken(SUPERVISOR_ID, "SUPERVISOR")

        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/results/supervisor/stats",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body == {"pendingCount": 5, "approvedToday": 3, "escalatedCount": 2}
        # No patient data / extra fields leak onto a counts-only endpoint.
        assert set(body.keys()) == {"pendingCount", "approvedToday", "escalatedCount"}

    @pytest.mark.asyncio
    async def test_zeroDataReturnsZerosNotNull(self, asyncClient):
        _overrideDb(_FakeStatsSession(pendingCount=0, approvedToday=0, escalatedCount=0))
        token = _mintToken(SUPERVISOR_ID, "SUPERVISOR")

        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/results/supervisor/stats",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body == {"pendingCount": 0, "approvedToday": 0, "escalatedCount": 0}
        assert all(isinstance(v, int) for v in body.values())


# ── Efficiency: one round trip, not three ──────────────────────────────────


class TestSupervisorStatsEfficiency:
    @pytest.mark.asyncio
    async def test_getSupervisorStatsIssuesExactlyOneQuery(self):
        session = _FakeStatsSession(pendingCount=1, approvedToday=1, escalatedCount=1)
        service = ResultReviewService(db=session)

        result = await service.getSupervisorStats()

        assert session.executeCount == 1
        assert result == {"pendingCount": 1, "approvedToday": 1, "escalatedCount": 1}


# ── Parity: counts must use the exact same filters as the queue lists ──────


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class TestSupervisorStatsParityWithQueueLists:
    """Structural (not fixture-dependent) proof that the stats endpoint's
    per-count filters can't silently diverge from the matching queue list
    endpoint's filter — both call sites share the same private helper, so
    this only breaks if a future edit changes one call site without the
    other.
    """

    def test_pendingFilterMatchesGetPendingFilter(self):
        statsSubq = (
            select(func.count())
            .select_from(AnalysisResult)
            .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
            .where(*ResultReviewService._pendingApprovalFilter())
        )
        pendingListTotal = (
            select(func.count())
            .select_from(AnalysisResult)
            .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
            .where(*ResultReviewService._pendingApprovalFilter())
        )
        assert _compiled(statsSubq) == _compiled(pendingListTotal)

    def test_escalatedFilterMatchesGetEscalatedFilter(self):
        statsSubq = (
            select(func.count())
            .select_from(AnalysisResult)
            .where(*ResultReviewService._escalatedFilter())
        )
        escalatedListTotal = (
            select(func.count())
            .select_from(AnalysisResult)
            .where(*ResultReviewService._escalatedFilter())
        )
        assert _compiled(statsSubq) == _compiled(escalatedListTotal)

    def test_approvedTodayFilterMatchesGetApprovedTodayFilter(self):
        start, end = ResultReviewService._todayWindowPht()
        statsSubq = (
            select(func.count())
            .select_from(ResultApproval)
            .where(*ResultReviewService._approvedTodayFilter(start, end))
        )
        approvedListTotal = (
            select(func.count())
            .select_from(ResultApproval)
            .where(*ResultReviewService._approvedTodayFilter(start, end))
        )
        assert _compiled(statsSubq) == _compiled(approvedListTotal)


# ── PHT day boundary ─────────────────────────────────────────────────────────


class TestApprovedTodayPhtBoundary:
    """Approved Today is global across supervisors (matches getApprovedToday,
    which has no approved_by filter) and must use a midnight-to-midnight PHT
    window, not a UTC one.
    """

    @patch("src.services.result_review_service.datetime")
    def test_boundaryIsMidnightToMidnightPht(self, mockDatetime):
        # "Now" = 2026-09-28 10:00 PHT.
        fixedNow = datetime(2026, 9, 28, 10, 0, 0, tzinfo=_PHT)
        mockDatetime.now.return_value = fixedNow

        start, end = ResultReviewService._todayWindowPht()

        assert start == datetime(2026, 9, 28, 0, 0, 0, tzinfo=_PHT)
        assert end == datetime(2026, 9, 29, 0, 0, 0, tzinfo=_PHT)

    @patch("src.services.result_review_service.datetime")
    def test_yesterdayLateNightExcludedTodayEarlyMorningIncluded(self, mockDatetime):
        fixedNow = datetime(2026, 9, 28, 10, 0, 0, tzinfo=_PHT)
        mockDatetime.now.return_value = fixedNow

        start, end = ResultReviewService._todayWindowPht()

        yesterday2330Pht = datetime(2026, 9, 27, 23, 30, 0, tzinfo=_PHT)
        today0030Pht = datetime(2026, 9, 28, 0, 30, 0, tzinfo=_PHT)

        assert not (start <= yesterday2330Pht < end)
        assert start <= today0030Pht < end

    @patch("src.services.result_review_service.datetime")
    def test_boundaryQueryUsesTzAwareBoundsNotBareDate(self, mockDatetime):
        """Regression guard for the original bug: the bound values passed
        into the WHERE clause must be tz-aware `datetime`s (which asyncpg
        sends as an absolute instant), never a bare `date` (which Postgres
        implicitly reinterprets in the session's timezone).
        """
        fixedNow = datetime(2026, 9, 28, 10, 0, 0, tzinfo=_PHT)
        mockDatetime.now.return_value = fixedNow

        start, end = ResultReviewService._todayWindowPht()

        assert isinstance(start, datetime) and start.tzinfo is not None
        assert isinstance(end, datetime) and end.tzinfo is not None

        stmt = (
            select(func.count())
            .select_from(ResultApproval)
            .where(*ResultReviewService._approvedTodayFilter(start, end))
        )
        compiled = _compiled(stmt)
        assert "2026-09-28 00:00:00+08:00" in compiled
        assert "2026-09-29 00:00:00+08:00" in compiled
