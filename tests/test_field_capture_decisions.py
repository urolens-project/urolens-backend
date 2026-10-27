"""Unit tests — the phone's field decisions (multi-field, mobile lane).

- Proceed (`acceptField`): ANALYZED to ACCEPTED while CAPTURING; `field_count`
  and the image pointer follow; a repeat is harmless; a discarded field cannot
  come back.
- Discard while capturing (`discardField`, mobile path): reason optional, an
  ANALYZED or ACCEPTED field may go; the field and its image become DISCARDED.
- Submit for Verification (`submitFields`): CAPTURING to PENDING_CONFIRM, refused
  while any field is undecided, then below the minimum; nothing is discarded. A
  result whose supervisor return is still open goes back to RETURNED_FOR_CORRECTION.
- Every action takes the specimen lock, checks ownership and the rejected
  specimen, audits and commits once; a refusal commits nothing.
The session is the stateful fake from test_field_corrections.py.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from src.api.fields import getResultFieldService
from src.core.config import settings
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import ImageStatus
from src.models.result_field import FieldStatus
from src.schemas.field import ResultStatusResponse
from src.services.result_field_service import ResultFieldService
from tests.test_field_corrections import (
    MEDTECH_ID,
    NOW,
    OTHER_ID,
    RESULT_ID,
    SPECIMEN_ID,
    CorrectionDb,
    mkField,
    mkResult,
    mkSpecimen,
)

NOT_CAPTURING = [
    ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION,
    ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED, ResultStatus.RELEASED,
    ResultStatus.CRITICAL_ESCALATED, ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.FAILED,
]


class DecisionDb(CorrectionDb):
    """`CorrectionDb` that also answers submit's queries and a missing result.

    `openReturn` is the reason of the result's open supervisor return, if any.
    """

    def __init__(self, field, *, resultMissing: bool = False, openReturn: str | None = None, **kw):
        super().__init__(field, **kw)
        self.resultMissing = resultMissing
        self.openReturn = openReturn

    async def get(self, model, key, **kwargs):
        if model is AnalysisResult and self.resultMissing:
            return None
        return await super().get(model, key, **kwargs)

    async def execute(self, stmt):
        low = " ".join(str(stmt).lower().split())
        if low.startswith("select result_fields.status from"):
            self.statements.append(str(stmt))
            out = MagicMock()
            out.scalars.return_value.all.return_value = [f.status for f in self.fields]
            return out
        if "from result_returns" in low:
            self.statements.append(low)
            out = MagicMock()
            out.all.return_value = [(self.result.resultId, self.openReturn)] if self.openReturn else []
            return out
        return await super().execute(stmt)


@pytest.fixture(autouse=True)
def noStorage():
    with patch("src.services.result_field_service.signedImageUrls", AsyncMock(return_value={})):
        yield


def _service(db) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=MagicMock(record=AsyncMock()), _notifService=MagicMock())


def _capturing(**kw):
    return mkResult(ResultStatus.CAPTURING, **kw)


def _fields(accepted: int, *, analyzed: int = 0, discarded: int = 0):
    statuses = [FieldStatus.ACCEPTED] * accepted + [FieldStatus.ANALYZED] * analyzed + [FieldStatus.DISCARDED] * discarded
    return [mkField(n, status=s) for n, s in enumerate(statuses, start=1)]


# ── acceptField ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proceedAcceptsTheFieldAndUpdatesTheRunningTotals():
    fields = [mkField(1, status=FieldStatus.DISCARDED), mkField(2, status=FieldStatus.ANALYZED),
              mkField(3, status=FieldStatus.ANALYZED)]
    db = DecisionDb(fields[1], fields=fields, result=_capturing(fieldCount=0, imageId=fields[0].imageId))
    service = _service(db)

    response = await service.acceptField(fields[1].fieldId, MEDTECH_ID, MagicMock())

    assert fields[1].status == FieldStatus.ACCEPTED and fields[1].version == 2
    assert db.result.fieldCount == 1 and db.result.imageId == fields[1].imageId
    assert db.result.status == ResultStatus.CAPTURING
    assert response.field.status == FieldStatus.ACCEPTED
    assert (response.acceptedFieldCount, response.undecidedFieldCount) == (1, 1)
    assert db.lockedSpecimen
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELD_ACCEPTED" and kwargs["entityId"] == RESULT_ID
    assert kwargs["detailJson"] == {
        "specimen_id": str(SPECIMEN_ID), "field_id": str(fields[1].fieldId),
        "field_no": 2, "accepted_field_count": 1,
    }
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_acceptingALaterFieldKeepsTheImagePointerOnTheFirstAccepted():
    fields = [mkField(1), mkField(2, status=FieldStatus.ANALYZED)]
    db = DecisionDb(fields[1], fields=fields, result=_capturing(fieldCount=1, imageId=fields[0].imageId))

    await _service(db).acceptField(fields[1].fieldId, MEDTECH_ID, MagicMock())

    assert db.result.fieldCount == 2 and db.result.imageId == fields[0].imageId


@pytest.mark.asyncio
async def test_acceptingAnAcceptedFieldAgainChangesNothing():
    field = mkField(1)
    db = DecisionDb(field, result=_capturing())
    service = _service(db)

    response = await service.acceptField(field.fieldId, MEDTECH_ID, MagicMock())

    assert response.field.status == FieldStatus.ACCEPTED and field.version == 1
    service.auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aDiscardedFieldCannotBeAccepted():
    field = mkField(1, status=FieldStatus.DISCARDED)
    db = DecisionDb(field, result=_capturing(fieldCount=0))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).acceptField(field.fieldId, MEDTECH_ID, MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "INVALID_FIELD_STATUS")
    assert field.status == FieldStatus.DISCARDED and db.result.fieldCount == 0
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NOT_CAPTURING)
async def test_proceedIsRefusedUnlessTheResultIsCapturing(status):
    field = mkField(1, status=FieldStatus.ANALYZED)
    db = DecisionDb(field, result=mkResult(status))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).acceptField(field.fieldId, MEDTECH_ID, MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "INVALID_RESULT_STATUS")
    assert field.status == FieldStatus.ANALYZED
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("specimen", "code"), [
    (mkSpecimen(medtechId=OTHER_ID), "SPECIMEN_NOT_ASSIGNED"),
    (mkSpecimen(status="REJECTED"), "SPECIMEN_REJECTED"),
])
async def test_proceedChecksTheSpecimen(specimen, code):
    field = mkField(1, status=FieldStatus.ANALYZED)
    db = DecisionDb(field, result=_capturing(), specimen=specimen)

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).acceptField(field.fieldId, MEDTECH_ID, MagicMock())

    assert excInfo.value.errorCode == code
    assert field.status == FieldStatus.ANALYZED
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_proceedOnAnUnknownFieldIsNotFound():
    db = DecisionDb(None, result=_capturing())
    with pytest.raises(HTTPException) as excInfo:
        await _service(db).acceptField(uuid.uuid4(), MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (404, "FIELD_NOT_FOUND")


# ── discardField: the capturing (mobile) path ────────────────────────────────

@pytest.mark.asyncio
async def test_discardingAnUndecidedFieldNeedsNoReason():
    fields = [mkField(1), mkField(2, status=FieldStatus.ANALYZED)]
    db = DecisionDb(fields[1], fields=fields, result=_capturing(fieldCount=1, imageId=fields[0].imageId))
    service = _service(db)

    response = await service.discardField(fields[1].fieldId, MEDTECH_ID, None, MagicMock())

    assert fields[1].status == FieldStatus.DISCARDED and fields[1].discardReason is None
    assert fields[1].version == 2
    image = db.images[fields[1].imageId]
    assert image.status == ImageStatus.DISCARDED and image.discardedAt is not None
    assert db.images[fields[0].imageId].status == ImageStatus.ACTIVE
    assert db.result.fieldCount == 1 and db.result.imageId == fields[0].imageId
    assert db.result.status == ResultStatus.CAPTURING
    assert (response.acceptedFieldCount, response.undecidedFieldCount) == (1, 0)
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELD_DISCARDED"
    assert kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID), "field_id": str(fields[1].fieldId)}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_discardingAnAcceptedFieldLowersTheCountAndMovesThePointer():
    fields = [mkField(1), mkField(2)]
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=2, imageId=fields[0].imageId))

    await _service(db).discardField(fields[0].fieldId, MEDTECH_ID, "  Same view as field 2  ", MagicMock())

    assert fields[0].status == FieldStatus.DISCARDED
    assert fields[0].discardReason == "Same view as field 2"
    assert db.result.fieldCount == 1 and db.result.imageId == fields[1].imageId


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_aBlankReasonIsStoredAsNoReasonWhileCapturing(reason):
    field = mkField(1, status=FieldStatus.ANALYZED)
    db = DecisionDb(field, result=_capturing(fieldCount=0))

    await _service(db).discardField(field.fieldId, MEDTECH_ID, reason, MagicMock())

    assert field.status == FieldStatus.DISCARDED and field.discardReason is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_discardingADiscardedFieldWhileCapturingChangesNothing():
    field = mkField(1, status=FieldStatus.DISCARDED)
    db = DecisionDb(field, result=_capturing(fieldCount=0))
    service = _service(db)

    response = await service.discardField(field.fieldId, MEDTECH_ID, None, MagicMock())

    assert response.field.status == FieldStatus.DISCARDED and field.version == 1
    service.auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_discardWhileCapturingChecksTheSpecimen():
    field = mkField(1, status=FieldStatus.ANALYZED)
    db = DecisionDb(field, result=_capturing(), specimen=mkSpecimen(medtechId=OTHER_ID))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).discardField(field.fieldId, MEDTECH_ID, None, MagicMock())

    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    assert field.status == FieldStatus.ANALYZED
    db.commit.assert_not_awaited()


# ── submitFields ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submittingTenAcceptedFieldsSendsTheResultToVerification():
    fields = _fields(10, discarded=2)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=10, imageId=fields[0].imageId))
    service = _service(db)

    response = await service.submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert response == ResultStatusResponse(resultId=RESULT_ID, status=ResultStatus.PENDING_CONFIRM, updatedAt=NOW)
    assert db.result.status == ResultStatus.PENDING_CONFIRM and db.result.fieldCount == 10
    assert all(f.status != FieldStatus.ANALYZED for f in fields)
    assert db.lockedSpecimen
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELDS_SUBMITTED" and kwargs["entityId"] == RESULT_ID
    assert kwargs["detailJson"] == {
        "specimen_id": str(SPECIMEN_ID), "accepted_field_count": 10, "discarded_field_count": 2,
        "status": "PENDING_CONFIRM",
    }
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aResultSentBackForMoreFieldsReturnsToTheReturnedState():
    # Supervisor returned it, the website reopened capture, the phone added fields.
    fields = _fields(12)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=12),
                    openReturn="Casts unclear, capture more LPF fields")
    service = _service(db)

    response = await service.submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert response.status == ResultStatus.RETURNED_FOR_CORRECTION
    assert db.result.status == ResultStatus.RETURNED_FOR_CORRECTION and db.result.fieldCount == 12
    detail = service.auditLogger.record.call_args.kwargs["detailJson"]
    assert detail["status"] == "RETURNED_FOR_CORRECTION"
    assert "Casts unclear" not in str(detail)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_submitAsksOnlyAboutThisResultsOpenReturn():
    fields = _fields(10)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=10))

    await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    [query] = [q for q in db.statements if "from result_returns" in q]
    assert "left outer join result_confirmations" in query
    assert "result_returns.returned_at > result_confirmations.confirmed_at" in query


@pytest.mark.asyncio
async def test_aRefusedSubmitDoesNotLookForAReturn():
    fields = _fields(9)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=9), openReturn="More fields")

    with pytest.raises(HTTPException):
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert not [q for q in db.statements if "from result_returns" in q]
    assert db.result.status == ResultStatus.CAPTURING


@pytest.mark.asyncio
async def test_thereIsNoMaximum():
    fields = _fields(25)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=25))

    await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert db.result.status == ResultStatus.PENDING_CONFIRM and db.result.fieldCount == 25


@pytest.mark.asyncio
async def test_submitIsRefusedWhileAnyFieldIsUndecided():
    fields = _fields(12, analyzed=2)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=12))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELDS_UNDECIDED")
    assert excInfo.value.detail == "2 fields not reviewed."
    assert db.result.status == ResultStatus.CAPTURING
    assert [f.status for f in fields].count(FieldStatus.ANALYZED) == 2   # nothing discarded for them
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_undecidedFieldsAreReportedBeforeTheMinimum():
    fields = _fields(3, analyzed=1)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=3))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert excInfo.value.errorCode == "FIELDS_UNDECIDED"
    assert excInfo.value.detail == "1 field not reviewed."


@pytest.mark.asyncio
async def test_submitIsRefusedBelowTheMinimumAndDiscardedFieldsDoNotCount():
    fields = _fields(9, discarded=3)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=9))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELDS_BELOW_MINIMUM")
    assert excInfo.value.detail == "9 of 10 required fields accepted."
    assert db.result.status == ResultStatus.CAPTURING
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_theMinimumComesFromSettings(monkeypatch):
    monkeypatch.setattr(settings, "minFieldsPerSpecimen", 3)
    fields = _fields(3)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=3))

    await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert db.result.status == ResultStatus.PENDING_CONFIRM


@pytest.mark.asyncio
@pytest.mark.parametrize("status", NOT_CAPTURING)
async def test_submitIsRefusedUnlessTheResultIsCapturing(status):
    fields = _fields(10)
    db = DecisionDb(fields[0], fields=fields, result=mkResult(status, fieldCount=10))

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "INVALID_RESULT_STATUS")
    assert db.result.status == status
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("specimen", "code"), [
    (mkSpecimen(medtechId=OTHER_ID), "SPECIMEN_NOT_ASSIGNED"),
    (mkSpecimen(status="REJECTED"), "SPECIMEN_REJECTED"),
])
async def test_submitChecksTheSpecimen(specimen, code):
    fields = _fields(10)
    db = DecisionDb(fields[0], fields=fields, result=_capturing(fieldCount=10), specimen=specimen)

    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(RESULT_ID, MEDTECH_ID, MagicMock())

    assert excInfo.value.errorCode == code
    assert db.result.status == ResultStatus.CAPTURING
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submittingAnUnknownResultIsNotFound():
    db = DecisionDb(None, resultMissing=True)
    with pytest.raises(HTTPException) as excInfo:
        await _service(db).submitFields(uuid.uuid4(), MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (404, "RESULT_NOT_FOUND")


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def routeClient(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
    yield TestClient(app)
    for dependency in (getCurrentUser, getResultFieldService):
        app.dependency_overrides.pop(dependency, None)


def _routeTo(fields, result) -> DecisionDb:
    db = DecisionDb(fields[0], fields=fields, result=result)
    app.dependency_overrides[getResultFieldService] = lambda: _service(db)
    return db


def test_theAcceptRouteRunsTheServiceAsTheCaller(routeClient):
    fields = [mkField(1, status=FieldStatus.ANALYZED)]
    db = _routeTo(fields, _capturing(fieldCount=0))

    response = routeClient.post(f"/api/v1/fields/{fields[0].fieldId}/accept")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["field"]["status"] == "ACCEPTED" and body["acceptedFieldCount"] == 1
    db.commit.assert_awaited_once()


def test_theDiscardRouteTakesTheCapturingPathWithoutABody(routeClient):
    fields = [mkField(1, status=FieldStatus.ANALYZED)]
    _routeTo(fields, _capturing(fieldCount=0))

    response = routeClient.post(f"/api/v1/fields/{fields[0].fieldId}/discard")

    assert response.status_code == 200, response.text
    assert response.json()["field"]["status"] == "DISCARDED"


def test_theSubmitRouteAnswersInTheStatusShape(routeClient):
    _routeTo(_fields(10), _capturing(fieldCount=10))

    response = routeClient.post(f"/api/v1/results/{RESULT_ID}/fields/submit")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "resultId": str(RESULT_ID), "status": "PENDING_CONFIRM", "updatedAt": "2026-10-27T08:00:00Z",
    }


def test_aRefusedSubmitUsesTheErrorEnvelope(routeClient):
    _routeTo(_fields(10, analyzed=3), _capturing(fieldCount=10))

    response = routeClient.post(f"/api/v1/results/{RESULT_ID}/fields/submit")

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "FIELDS_UNDECIDED", "message": "3 fields not reviewed."}}
