"""Unit tests — src.core.audit_logger.AuditLogger.record (UROLENS-222, audit F-11).

Two write paths:
- with a session (every service): the row joins the caller's transaction, so
  it can't be silently lost — it commits or rolls back with the action;
- without one (auth-flow helpers): best-effort Supabase insert whose failure
  is logged at ERROR with the `AUDIT_WRITE_FAILED` marker and never raised.
"""
from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import audit_logger
from src.core.audit_logger import AUDIT_WRITE_FAILED, AuditLogger
from src.models.audit_log import AuditLog

ENTITY_ID = uuid.UUID("00000000-0000-0000-0000-000000000091")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000092")


def _makeSession() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()  # AsyncSession.add is synchronous
    return db


def _makeSupabase(insertError: Exception | None = None) -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock(side_effect=insertError)
    return sb


# ── Session path ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_withASessionTheRowIsAddedToTheCallersTransaction():
    db = _makeSession()
    sb = _makeSupabase()
    request = MagicMock()
    request.client.host = "10.0.0.7"

    with patch.object(audit_logger, "supabase", sb):
        await AuditLogger().record(
            eventType="RESULT_OVERRIDDEN",
            entityType="analysis_result",
            entityId=ENTITY_ID,
            userId=USER_ID,
            db=db,
            detailJson={"parameter": "rbc_casts"},
            request=request,
        )

    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert isinstance(row, AuditLog)
    assert (row.eventType, row.entityType, row.entityId, row.userId) == (
        "RESULT_OVERRIDDEN", "analysis_result", ENTITY_ID, USER_ID,
    )
    assert row.detailJson == {"parameter": "rbc_casts"}
    assert row.ipAddress == "10.0.0.7"
    sb.table.assert_not_called()  # never the separate, lossy path


@pytest.mark.asyncio
async def test_sessionPathAcceptsStringIdsAndNoUser():
    db = _makeSession()

    await AuditLogger().record(
        eventType="SMART_DIAGNOSIS_GENERATED",
        entityType="analysis_result",
        entityId=str(ENTITY_ID),
        userId=None,
        db=db,
    )

    row = db.add.call_args.args[0]
    assert row.entityId == ENTITY_ID
    assert row.userId is None
    assert row.detailJson == {}


@pytest.mark.asyncio
async def test_sessionPathRaisesOnABadIdInsteadOfWritingAnUnusableRow():
    # A raise here fails the caller's request — preferable to an action
    # whose audit row can't be stored.
    with pytest.raises(ValueError):
        await AuditLogger().record(
            eventType="X", entityType="y", entityId="not-a-uuid", userId=None, db=_makeSession()
        )


# ── Best-effort path (no session) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_withoutASessionTheRowIsWrittenThroughSupabase():
    sb = _makeSupabase()

    with patch.object(audit_logger, "supabase", sb):
        await AuditLogger().record(
            eventType="LOGIN_SUCCESS", entityType="auth", entityId=USER_ID, userId=USER_ID,
            ipAddress="10.0.0.8",
        )

    sb.table.assert_called_once_with("audit_logs")
    inserted = sb.table.return_value.insert.call_args.args[0]
    assert inserted["event_type"] == "LOGIN_SUCCESS"
    assert inserted["ip_address"] == "10.0.0.8"


@pytest.mark.asyncio
async def test_aLostBestEffortWriteIsLoggedAtErrorWithTheAlertMarkerAndNotRaised(caplog):
    sb = _makeSupabase(insertError=RuntimeError("supabase down"))

    with patch.object(audit_logger, "supabase", sb), caplog.at_level(logging.ERROR):
        await AuditLogger().record(
            eventType="ACCESS_DENIED", entityType="auth", entityId=USER_ID, userId=None
        )

    [entry] = [r for r in caplog.records if AUDIT_WRITE_FAILED in r.getMessage()]
    assert entry.levelno == logging.ERROR
    assert "ACCESS_DENIED" in entry.getMessage()
    assert entry.exc_info is not None
