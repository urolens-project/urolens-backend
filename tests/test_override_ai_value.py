"""Unit tests — overriding an AI-detected value (UROLENS-227).

- `OverrideRequest`: the rationale is required and not blank; the corrected
  value is a whole particle count from 0 to `MAX_OVERRIDE_COUNT` (no fractions,
  no Infinity); the parameter is also accepted as `parameterName` (what the
  web sends), while the API docs keep naming it `parameter`.
- `overrideParameter`: a correction equal to the parameter's current count (its
  latest override, else the AI's) is refused with `OVERRIDE_UNCHANGED`.
- A new image clears the result's overrides, so confirmation can't apply
  counts taken from the previous image; the upload's audit row records how many.
- `AnalysisResult.manualOverrides` is ordered oldest first, so confirmation
  settles each parameter on its latest override.
- `GET /sync/pull` sends the overrides on the MedTech's results (any author),
  all on a full sync and only new ones on a delta.
DB is mocked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import Delete, Select

from src.core.audit_logger import AuditLogger
from src.core.enums import UserRole
from src.core.exceptions import UnprocessableException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.specimen import Specimen
from src.schemas.result_review import MAX_OVERRIDE_COUNT, OverrideRequest
from src.services import sync_service
from src.services.ai_integration_service import AIIntegrationService
from src.services.manual_override_service import ManualOverrideService
from tests.conftest import makeSyncDb

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000002a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000002a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000002a3")


# ── OverrideRequest ───────────────────────────────────────────────────────────

def _body(**overrides: object) -> dict:
    return {"parameter": "erythrocytes", "correctedValue": 4, "rationale": "Recounted", **overrides}


def test_maxOverrideCountIsThreeHundred() -> None:
    assert MAX_OVERRIDE_COUNT == 300


@pytest.mark.parametrize("value", [0, 1, MAX_OVERRIDE_COUNT, 5.0])
def test_wholeCountsFromZeroToTheMaximumAreAccepted(value: float) -> None:
    assert OverrideRequest(**_body(correctedValue=value)).correctedValue == int(value)


@pytest.mark.parametrize(
    "value", [-1, MAX_OVERRIDE_COUNT + 1, 3.7, float("inf"), float("nan"), 1e308, "many", None]
)
def test_fractionsNegativesOverTheMaximumAndNonNumbersAreRejected(value: object) -> None:
    with pytest.raises(ValidationError):
        OverrideRequest(**_body(correctedValue=value))


@pytest.mark.parametrize("rationale", ["", "   ", None])
def test_aBlankOrMissingRationaleIsRejected(rationale: str | None) -> None:
    with pytest.raises(ValidationError):
        OverrideRequest(**_body(rationale=rationale))


def test_omittingTheRationaleIsRejected() -> None:
    body = _body()
    del body["rationale"]

    with pytest.raises(ValidationError):
        OverrideRequest(**body)


def test_rationaleIsStripped() -> None:
    assert OverrideRequest(**_body(rationale="  Recounted  ")).rationale == "Recounted"


def test_parameterIsAlsoAcceptedAsParameterName() -> None:
    # The web sends `parameter_name`; its case-conversion bridge makes it `parameterName`.
    body = _body()
    body["parameterName"] = body.pop("parameter")

    assert OverrideRequest(**body).parameter == "erythrocytes"


def test_parameterWinsWhenBothNamesAreSent() -> None:
    assert OverrideRequest(**_body(parameterName="leukocytes")).parameter == "erythrocytes"


@pytest.mark.parametrize("otherName", ["parameter_name", "param"])
def test_noOtherNameForTheParameterIsAccepted(otherName: str) -> None:
    body = _body()
    body[otherName] = body.pop("parameter")

    with pytest.raises(ValidationError):
        OverrideRequest(**body)


def test_theApiDocsStillNameTheFieldParameter() -> None:
    from main import app

    schema = app.openapi()["components"]["schemas"]["OverrideRequest"]
    assert "parameter" in schema["properties"]
    assert "parameterName" not in schema["properties"]
    assert "parameter" in schema["required"]


# ── overrideParameter: refuse a correction that changes nothing ───────────────

def _makeResult() -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_CONFIRM
    result.aiFindings = {"erythrocytes": 7}
    result.particleClasses = {}
    return result


def _makeOverrideDb(latestOverride: str | None) -> tuple[AsyncMock, list]:
    specimen = MagicMock(spec=Specimen)
    specimen.medtechId = MEDTECH_ID
    result = _makeResult()
    overrideQueries: list = []

    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        if entity is ManualOverride:
            overrideQueries.append(stmt)
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = latestOverride if entity is ManualOverride else result
        return executeResult

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(return_value=specimen)
    db.add = MagicMock()
    return db, overrideQueries


async def _override(db: AsyncMock, correctedValue: int, auditLogger: MagicMock | None = None) -> ManualOverride:
    if auditLogger is None:
        auditLogger = MagicMock(spec=AuditLogger, record=AsyncMock())
    return await ManualOverrideService(db=db, auditLogger=auditLogger).overrideParameter(
        resultId=RESULT_ID,
        parameter="erythrocytes",
        correctedValue=correctedValue,
        rationale="Recounted",
        originalAiValue=None,
        medtechId=MEDTECH_ID,
        callerRole=UserRole.MEDTECH,
        request=MagicMock(spec=Request),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latestOverride", "correctedValue"),
    [(None, 7), ("5.0", 5)],
    ids=["same-as-the-AI-value", "same-as-the-latest-override"],
)
async def test_aCorrectionEqualToTheCurrentCountIsRefused(
    latestOverride: str | None, correctedValue: int
) -> None:
    db, _ = _makeOverrideDb(latestOverride)
    auditLogger = MagicMock(spec=AuditLogger, record=AsyncMock())

    with pytest.raises(UnprocessableException) as excInfo:
        await _override(db, correctedValue, auditLogger)

    assert excInfo.value.status_code == 422
    assert excInfo.value.errorCode == "OVERRIDE_UNCHANGED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_changingBackToTheAiValueAfterAnOverrideIsAllowed() -> None:
    # AI said 7, a first override set 5: setting 7 again is a real change.
    db, _ = _makeOverrideDb(latestOverride="5.0")

    override = await _override(db, correctedValue=7)

    assert override.correctedValue == "7.0"
    assert override.originalAiValue == "7.0"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_theCurrentCountIsTheParametersLatestOverride() -> None:
    db, overrideQueries = _makeOverrideDb(latestOverride=None)

    await _override(db, correctedValue=4)

    [stmt] = overrideQueries
    sql = str(stmt)
    assert "manual_overrides.result_id" in sql
    assert "manual_overrides.parameter_name" in sql
    assert "ORDER BY manual_overrides.overridden_at DESC" in sql
    assert "LIMIT" in sql


# ── A new image clears the result's overrides ─────────────────────────────────

@pytest.mark.asyncio
async def test_clearingOverridesDeletesOnlyThisResultsRowsAndCountsThem() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=2))
    service = AIIntegrationService(db=db, auditLogger=MagicMock())

    cleared = await service._clearManualOverrides(RESULT_ID)

    assert cleared == 2
    [stmt] = [c.args[0] for c in db.execute.await_args_list]
    assert isinstance(stmt, Delete)
    assert stmt.table.name == "manual_overrides"
    assert str(stmt.whereclause) == "manual_overrides.result_id = :result_id_1"
    assert stmt.whereclause.right.value == RESULT_ID


@pytest.mark.asyncio
async def test_clearingWhenThereAreNoOverridesCountsZero() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=0))

    assert await AIIntegrationService(db=db, auditLogger=MagicMock())._clearManualOverrides(RESULT_ID) == 0


@pytest.mark.asyncio
async def test_uploadClearsTheResultsOverridesAndAuditsHowMany() -> None:
    db = AsyncMock()
    db.add = MagicMock()
    auditLogger = MagicMock(spec=AuditLogger, record=AsyncMock())
    service = AIIntegrationService(db=db, auditLogger=auditLogger)
    result = _makeResult()
    upload = MagicMock(content_type="image/jpeg")

    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"jpeg"),
        _requireUploadAllowed=AsyncMock(),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"jpeg"),
        _infer=AsyncMock(return_value={}),
        _replacePreviousImage=AsyncMock(),
        _uploadToStorage=AsyncMock(),
        _getOrCreateResult=AsyncMock(return_value=result),
        _clearManualOverrides=AsyncMock(return_value=2),
    ):
        await service.handleUpload(specimenId=SPECIMEN_ID, uploaderId=MEDTECH_ID, file=upload, request=None)
        service._clearManualOverrides.assert_awaited_once_with(RESULT_ID)

    auditLogger.record.assert_awaited_once()
    assert auditLogger.record.await_args.kwargs["detailJson"]["manual_overrides_cleared"] == 2
    db.commit.assert_awaited_once()


# ── The latest override is the one confirmation keeps ─────────────────────────

def test_manualOverridesAreOrderedOldestFirst() -> None:
    [orderBy] = AnalysisResult.manualOverrides.property.order_by
    assert orderBy.key == "overridden_at"
    assert orderBy.table.name == "manual_overrides"


# ── Sync sends overrides to the phone ─────────────────────────────────────────

def _syncSupabase(specimenIds: list[str]) -> MagicMock:
    def _table(name: str) -> MagicMock:
        query = MagicMock()
        for method in ("select", "eq", "gt", "in_"):
            getattr(query, method).return_value = query
        rows = [{"specimen_id": s} for s in specimenIds] if name == "specimens" else []
        query.execute = AsyncMock(return_value=MagicMock(data=rows))
        return query

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _storedOverride() -> ManualOverride:
    return ManualOverride(
        overrideId=uuid.UUID("00000000-0000-0000-0000-0000000002c1"),
        resultId=RESULT_ID,
        parameterName="erythrocytes",
        originalAiValue="7.0",
        correctedValue="4.0",
        rationale="Recounted",
        medtechId=MEDTECH_ID,
        overriddenAt=datetime(2026, 9, 29, 8, 0, tzinfo=UTC),
    )


async def _syncPull(db: AsyncMock, specimenIds: list[str], since: datetime | None = None) -> dict:
    auditLogger = MagicMock(record=AsyncMock())
    with patch.object(sync_service, "supabase", _syncSupabase(specimenIds)), \
         patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        payload = await sync_service.pull(db, str(MEDTECH_ID), since)
    return {"payload": payload, "auditLogger": auditLogger}


def _overrideQuery(db: AsyncMock) -> Select:
    [stmt] = [
        c.args[0] for c in db.execute.await_args_list
        if c.args[0].column_descriptions[0]["entity"] is ManualOverride
    ]
    return stmt


@pytest.mark.asyncio
async def test_fullSyncSendsEveryOverrideOnTheMedtechsResultsAsNumbers() -> None:
    db = makeSyncDb(overrides=[_storedOverride()])

    pulled = await _syncPull(db, [str(SPECIMEN_ID)])

    changes = pulled["payload"]["changes"]["manualOverrides"]
    assert changes["updated"] == []
    assert changes["created"] == [{
        "id": "00000000-0000-0000-0000-0000000002c1",
        "result_id": str(RESULT_ID),
        "parameter_name": "erythrocytes",
        "original_ai_value": 7.0,
        "corrected_value": 4.0,
        "rationale": "Recounted",
        "medtech_id": str(MEDTECH_ID),
        "overridden_at": "2026-09-29T08:00:00+00:00",
    }]
    sql = str(_overrideQuery(db))
    assert "JOIN analysis_results ON manual_overrides.result_id = analysis_results.result_id" in sql
    assert "analysis_results.specimen_id IN" in sql
    assert "ORDER BY manual_overrides.overridden_at" in sql
    assert "overridden_at >" not in sql


@pytest.mark.asyncio
async def test_deltaSyncSendsOnlyOverridesAddedSinceTheLastSyncAsUpdates() -> None:
    db = makeSyncDb(overrides=[_storedOverride()])
    since = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)

    pulled = await _syncPull(db, [str(SPECIMEN_ID)], since)

    changes = pulled["payload"]["changes"]["manualOverrides"]
    assert (len(changes["created"]), len(changes["updated"])) == (0, 1)
    stmt = _overrideQuery(db)
    assert "manual_overrides.overridden_at >" in str(stmt)
    assert since in stmt.compile().params.values()


@pytest.mark.asyncio
async def test_aSyncCarryingOnlyOverridesIsStillAudited() -> None:
    # Delta where only a supervisor's correction changed: no specimen or result rows.
    db = makeSyncDb(overrides=[_storedOverride()])

    pulled = await _syncPull(db, [str(SPECIMEN_ID)], since=datetime(2026, 9, 29, 7, 0, tzinfo=UTC))

    kwargs = pulled["auditLogger"].record.await_args.kwargs
    assert kwargs["detailJson"]["override_ids"] == ["00000000-0000-0000-0000-0000000002c1"]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_noOverrideQueryForAMedtechWithNoSpecimens() -> None:
    db = makeSyncDb()

    pulled = await _syncPull(db, [])

    assert pulled["payload"]["changes"]["manualOverrides"] == {"created": [], "updated": []}
    db.execute.assert_not_awaited()
