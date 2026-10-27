"""Unit tests — open supervisor returns (`open_returns`) and their reason in sync.

- `openReturnReasons`: no query for no results; one query that keeps returns
  newer than the result's confirmation (or with none), newest reason first.
- `GET /sync/pull`: a CAPTURING result (sent back for more fields) carries its
  open return's reason; returned results keep the existing query; other
  statuses carry none and run no extra query.
DB is mocked; the SQL rule itself was also exercised on a scratch Postgres.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from src.models.result_return import ResultReturn
from src.services import sync_service
from src.services.open_returns import openReturnReasons
from tests.conftest import makeSyncDb, syncQueries, syncResult, syncSpecimen

RESULT_A = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
RESULT_B = uuid.UUID("00000000-0000-0000-0000-0000000005b1")


def _sql(statement: object) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def _db(rows: list[tuple]) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


# ── openReturnReasons ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_noResultsNeedNoQuery():
    db = _db([])
    assert await openReturnReasons(db, []) == {}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_onlyReturnsNewerThanTheConfirmationAreOpen():
    db = _db([])

    await openReturnReasons(db, [RESULT_A])

    sql = _sql(db.execute.await_args.args[0])
    assert "FROM result_returns LEFT OUTER JOIN result_confirmations" in sql
    assert ("result_confirmations.confirmed_at IS NULL OR "
            "result_returns.returned_at > result_confirmations.confirmed_at") in sql
    assert sql.endswith("ORDER BY result_returns.returned_at DESC")


@pytest.mark.asyncio
async def test_theNewestOpenReasonWinsPerResult():
    db = _db([(RESULT_A, "Second return"), (RESULT_B, "Only return"), (RESULT_A, "First return")])

    reasons = await openReturnReasons(db, [RESULT_A, RESULT_B])

    assert reasons == {RESULT_A: "Second return", RESULT_B: "Only return"}


# ── sync ─────────────────────────────────────────────────────────────────────

async def _pull(db: AsyncMock) -> dict:
    with patch.object(sync_service, "AuditLogger", return_value=MagicMock(record=AsyncMock())):
        return await sync_service.pull(db, str(uuid.uuid4()), None)


def _returnQueries(db: AsyncMock) -> list[str]:
    return [_sql(q) for q in syncQueries(db, ResultReturn, "resultId")]


@pytest.mark.asyncio
async def test_aCapturingResultCarriesItsOpenReturnReason():
    specimen = syncSpecimen()
    result = syncResult(specimen, status="CAPTURING")
    db = makeSyncDb(rows=[(specimen, result)], reasons=[(result.resultId, "Casts unclear, capture more LPF fields")])

    payload = await _pull(db)

    [row] = payload["changes"]["analysisResults"]["created"]
    assert row["status"] == "CAPTURING"
    assert row["return_reason"] == "Casts unclear, capture more LPF fields"
    [query] = _returnQueries(db)
    assert "LEFT OUTER JOIN result_confirmations" in query


@pytest.mark.asyncio
async def test_aCapturingResultWithNoOpenReturnCarriesNone():
    specimen = syncSpecimen()
    result = syncResult(specimen, status="CAPTURING")
    db = makeSyncDb(rows=[(specimen, result)])

    payload = await _pull(db)

    assert payload["changes"]["analysisResults"]["created"][0]["return_reason"] is None


@pytest.mark.asyncio
async def test_returnedResultsKeepTheirOwnQuery():
    returned, capturing = syncSpecimen(), syncSpecimen()
    rows = [(returned, syncResult(returned, status="RETURNED_FOR_CORRECTION")),
            (capturing, syncResult(capturing, status="CAPTURING"))]
    db = makeSyncDb(rows=rows)

    await _pull(db)

    plain, joined = sorted(_returnQueries(db), key=lambda q: "JOIN" in q)
    assert "JOIN" not in plain and "LEFT OUTER JOIN result_confirmations" in joined


@pytest.mark.asyncio
async def test_otherStatusesRunNoReturnQuery():
    specimen = syncSpecimen()
    db = makeSyncDb(rows=[(specimen, syncResult(specimen, status="PENDING_CONFIRM"))])

    payload = await _pull(db)

    assert payload["changes"]["analysisResults"]["created"][0]["return_reason"] is None
    assert _returnQueries(db) == []
