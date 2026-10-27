"""Unit tests — the captured fields in `GET /sync/pull` (multi-field workflow).

- Flag off: the payload, its response JSON and the audit row are exactly as
  before; no field query runs.
- Flag on: `resultFields` carries every field of the window's results in the
  published `SyncResultField` shape (camelCase, every status, never the AI
  boxes, which are not even loaded); a delta sends fields changed since the
  last sync and deletes the fields of samples that left the window; the audit
  row lists the field IDs sent, also when only fields changed.
DB is mocked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from src.core.config import settings
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.schemas.sync import SyncPullResponse
from src.services import sync_service
from tests.conftest import (
    SYNC_MEDTECH_ID,
    makeSyncDb,
    syncQueries,
    syncResult,
    syncSpecimen,
)

SINCE = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)


def _field(resultId: uuid.UUID, specimenId: uuid.UUID, fieldNo: int, status: str = FieldStatus.ACCEPTED) -> ResultField:
    return ResultField(
        fieldId=uuid.uuid4(),
        resultId=resultId,
        specimenId=specimenId,
        imageId=uuid.uuid4(),
        fieldNo=fieldNo,
        magnification=Magnification.HPF,
        status=status,
        aiFindings={"erythrocytes": 3},
        effectiveCounts={"erythrocytes": 2},
        confidenceScores={"erythrocytes": 0.81},
        imageWidth=800,
        imageHeight=600,
        smartDiagnosis={"gout": {"level": "LOW"}},
        smartDiagnosisUnavailable=False,
        modelVersion="mvp-v1.0",
        version=2,
        createdAt=datetime(2026, 10, 27, 8, 0, tzinfo=UTC),
        updatedAt=datetime(2026, 10, 27, 8, 5, tzinfo=UTC),
    )


async def _pull(db: AsyncMock, since: datetime | None = None) -> tuple[dict, MagicMock]:
    auditLogger = MagicMock(record=AsyncMock())
    with patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        payload = await sync_service.pull(db, str(SYNC_MEDTECH_ID), since)
    return payload, auditLogger


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


# ── Flag off: unchanged ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_withTheFlagOffNothingAboutFieldsIsSentOrRead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    specimen = syncSpecimen()
    db = makeSyncDb(rows=[(specimen, syncResult(specimen))])

    payload, auditLogger = await _pull(db)

    assert set(payload["changes"]) == {"specimens", "queueAssignments", "analysisResults", "manualOverrides"}
    assert syncQueries(db, ResultField, "ResultField") == []
    assert "field_ids" not in auditLogger.record.await_args.kwargs["detailJson"]
    assert "resultFields" not in SyncPullResponse.model_validate(payload).model_dump(mode="json")["changes"]


# ── Flag on ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aFullSyncSendsEveryFieldInTheContractShapeWithoutBoxes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    specimen = syncSpecimen()
    result = syncResult(specimen, status="CAPTURING")
    accepted = _field(result.resultId, specimen.specimenId, 1)
    discarded = _field(result.resultId, specimen.specimenId, 2, FieldStatus.DISCARDED)
    db = makeSyncDb(rows=[(specimen, result)], fields=[accepted, discarded])

    payload, auditLogger = await _pull(db)

    changes = payload["changes"]["resultFields"]
    assert changes["updated"] == [] and changes["deleted"] == []
    first, second = changes["created"]
    assert first == {
        "fieldId": str(accepted.fieldId),
        "resultId": str(result.resultId),
        "specimenId": str(specimen.specimenId),
        "imageId": str(accepted.imageId),
        "fieldNo": 1,
        "magnification": "HPF",
        "status": "ACCEPTED",
        "aiFindings": {"erythrocytes": 3.0},
        "effectiveCounts": {"erythrocytes": 2.0},
        "confidenceScores": {"erythrocytes": 0.81},
        "imageWidth": 800,
        "imageHeight": 600,
        "smartDiagnosisPreview": {"gout": {"level": "LOW"}},
        "smartDiagnosisUnavailable": False,
        "version": 2,
        "createdAt": "2026-10-27T08:00:00Z",
        "updatedAt": "2026-10-27T08:05:00Z",
    }
    assert second["status"] == "DISCARDED"
    detail = auditLogger.record.await_args.kwargs["detailJson"]
    assert detail["field_ids"] == [str(accepted.fieldId), str(discarded.fieldId)]


@pytest.mark.asyncio
async def test_theFieldQueryNeverLoadsTheBoxesAndStaysInTheWindow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = makeSyncDb()

    await _pull(db)

    [query] = syncQueries(db, ResultField, "ResultField")
    sql = _sql(query)
    assert "result_fields.detections" not in sql
    assert "result_fields.specimen_id IN (SELECT specimens.specimen_id" in sql
    assert "specimens.medtech_id = " in sql
    assert sql.rstrip().endswith("ORDER BY result_fields.result_id, result_fields.field_no")


@pytest.mark.asyncio
async def test_aDeltaSendsFieldsChangedSinceTheLastSync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    specimen = syncSpecimen()
    field = _field(uuid.uuid4(), specimen.specimenId, 4)
    db = makeSyncDb(fields=[field])

    payload, auditLogger = await _pull(db, SINCE)

    [query] = syncQueries(db, ResultField, "ResultField")
    assert "result_fields.updated_at > " in _sql(query)
    changes = payload["changes"]["resultFields"]
    assert changes["created"] == [] and [f["fieldId"] for f in changes["updated"]] == [str(field.fieldId)]
    # Only a field changed (accepted, discarded, corrected): still audited.
    assert auditLogger.record.await_args.kwargs["detailJson"]["field_ids"] == [str(field.fieldId)]


@pytest.mark.asyncio
async def test_fieldsLeaveThePhoneWithTheirSample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    specimenId, resultId = uuid.uuid4(), uuid.uuid4()
    fieldIds = [uuid.uuid4(), uuid.uuid4()]
    db = makeSyncDb(removed=[(specimenId, resultId)], removedFieldIds=fieldIds)

    payload, _ = await _pull(db, SINCE)

    assert payload["changes"]["resultFields"]["deleted"] == [str(i) for i in fieldIds]
    [query] = syncQueries(db, ResultField, "fieldId")
    assert "result_fields.result_id IN" in _sql(query)


@pytest.mark.asyncio
async def test_withTheFlagOffRemovedSamplesDoNotLookUpFields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    db = makeSyncDb(removed=[(uuid.uuid4(), uuid.uuid4())])

    await _pull(db, SINCE)

    assert syncQueries(db, ResultField, "fieldId") == []


# ── The response JSON ─────────────────────────────────────────────────────────

def test_theResponseLeavesResultFieldsOutRatherThanNullWhenOff() -> None:
    empty = {"created": [], "updated": [], "deleted": []}
    tables = {"specimens": empty, "queueAssignments": empty, "analysisResults": empty, "manualOverrides": empty}

    off = SyncPullResponse.model_validate({"timestamp": "t", "changes": tables}).model_dump(mode="json")
    on = SyncPullResponse.model_validate(
        {"timestamp": "t", "changes": {**tables, "resultFields": empty}}
    ).model_dump(mode="json")

    assert "resultFields" not in off["changes"]
    assert on["changes"]["resultFields"] == empty
