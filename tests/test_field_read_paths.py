"""Unit tests — multi-field read paths (web lane, W1).

The field list and detail (`ResultFieldService`), the access rules
(`field_access`), the batched image signing (`core.storage`) and the field
counts on `GET /results/medtech/pending`. Sessions are fakes; no network, no DB.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from src.api.fields import getResultFieldService
from src.api.results import getConfirmationService
from src.core import storage
from src.core.config import settings
from src.core.exceptions import ForbiddenException, NotFoundException
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.field_annotation_edit import EditAction, FieldAnnotationEdit
from src.models.manual_override import ManualOverride
from src.models.patient import Patient
from src.models.result_field import FieldStatus, ResultField
from src.models.specimen import Specimen
from src.schemas.field import FieldDetail, FieldListResponse
from src.services.field_access import (
    loadFieldForWrite,
    readFieldAccess,
    requireFieldAccepted,
    requireVerificationPhase,
)
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_field_service import ResultFieldService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a3")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a4")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a5")
GOLDEN = Path(__file__).parent / "fixtures" / "medtech_pending_golden.json"
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)

MEDTECH = {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
SUPERVISOR = {"user_id": str(SUPERVISOR_ID), "role": "SUPERVISOR"}
OTHER_MEDTECH = {"user_id": str(OTHER_MEDTECH_ID), "role": "MEDTECH"}


# ── fakes ────────────────────────────────────────────────────────────────────

def _field(n: int, status=FieldStatus.ACCEPTED, effective=None, ai=None, conf=None, **kw) -> ResultField:
    fieldId = kw.pop("fieldId", uuid.UUID(f"00000000-0000-0000-0000-0000000006{n:02d}"))
    effective = effective if effective is not None else {"erythrocytes": 4}
    return ResultField(
        fieldId=fieldId, resultId=RESULT_ID, specimenId=SPECIMEN_ID,
        imageId=uuid.UUID(f"00000000-0000-0000-0000-0000000007{n:02d}"),
        fieldNo=n, magnification="HPF", status=status,
        aiFindings=ai if ai is not None else dict(effective),
        detections=kw.pop("detections", []), imageWidth=800, imageHeight=600,
        confidenceScores=conf or {}, effectiveCounts=effective,
        smartDiagnosis=None, smartDiagnosisUnavailable=False, modelVersion="mvp-v1.0",
        discardReason=None, verifiedBy=kw.pop("verifiedBy", None), verifiedAt=None,
        version=kw.pop("version", 1), createdAt=NOW, updatedAt=NOW, **kw,
    )


def _result(status=ResultStatus.PENDING_CONFIRM) -> AnalysisResult:
    result = AnalysisResult(resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=status)
    return result


def _specimen(medtechId=MEDTECH_ID, status="ASSIGNED") -> Specimen:
    return Specimen(specimenId=SPECIMEN_ID, medtechId=medtechId, status=status)


class FakeDb:
    """Routes `execute` by the compiled SQL and records every statement."""

    def __init__(self, *, result=None, specimen=None, fields=(), field=None, edits=(),
                 overrides=(), storageKeys=None, statusAfterLock=None):
        self.result = result if result is not None else _result()
        self.specimen = specimen if specimen is not None else _specimen()
        self.fields = list(fields)
        self.field = field
        self.edits = list(edits)
        self.overrides = list(overrides)
        self.storageKeys = storageKeys or {}
        self.statusAfterLock = statusAfterLock
        self.statements: list[str] = []
        self.commit = AsyncMock()
        self.lockedSpecimen = False

    async def get(self, model, key, **kwargs):
        if model is AnalysisResult:
            return self.result
        if model is Specimen:
            self.lockedSpecimen = self.lockedSpecimen or bool(kwargs.get("with_for_update"))
            return self.specimen
        return None

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        if low.startswith("update result_fields"):
            return out
        if "from result_fields join images" in low:
            out.all.return_value = [(f, self.storageKeys.get(f.fieldId, f"key-{f.fieldNo}")) for f in self.fields]
        elif "select analysis_results.status" in low:
            out.scalar_one.return_value = (
                self.statusAfterLock if self.statusAfterLock is not None else self.result.status
            )
        elif "select images.storage_key" in low:
            out.scalar_one_or_none.return_value = "key-detail"
        elif "select distinct field_annotation_edits.field_id" in low:
            out.scalars.return_value.all.return_value = list({e.fieldId for e in self.edits})
        elif "from field_annotation_edits" in low:
            out.scalars.return_value.all.return_value = self.edits
        elif "from manual_overrides" in low:
            out.scalars.return_value.all.return_value = self.overrides
        elif "from result_fields" in low and "result_fields.status =" in low:
            out.scalars.return_value.all.return_value = [
                f for f in self.fields if f.status == FieldStatus.ACCEPTED]
        elif "from result_fields" in low:
            out.scalar_one_or_none.return_value = self.field
        else:  # pragma: no cover - a query the fake does not know about
            raise AssertionError(f"unexpected statement: {low}")
        return out


def _service(db) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=MagicMock(record=AsyncMock()))


def _signed(urls=None):
    return patch("src.services.result_field_service.signedImageUrls",
                 AsyncMock(return_value=urls or {}))


def _updates(db: FakeDb) -> list[str]:
    return [s for s in db.statements if s.lower().lstrip().startswith("update result_fields")]


# ── field_access ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aMedtechReadsOnlyAnAssignedSpecimen():
    db = FakeDb()
    assert await readFieldAccess(db, SPECIMEN_ID, MEDTECH_ID, "MEDTECH") is db.specimen
    with pytest.raises(ForbiddenException) as excInfo:
        await readFieldAccess(db, SPECIMEN_ID, OTHER_MEDTECH_ID, "medtech")
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_aSupervisorReadsAnySpecimenAtAnyStatus():
    for status in ("PENDING_CONFIRM", "CAPTURING", "APPROVED", "RELEASED"):
        db = FakeDb(result=_result(status))
        assert await readFieldAccess(db, SPECIMEN_ID, SUPERVISOR_ID, "SUPERVISOR") is db.specimen


@pytest.mark.asyncio
async def test_aMedtechOnAMissingSpecimenIsForbiddenNotNotFound():
    db = FakeDb()
    db.specimen = None
    with pytest.raises(ForbiddenException):
        await readFieldAccess(db, SPECIMEN_ID, MEDTECH_ID, "MEDTECH")


@pytest.mark.asyncio
async def test_loadFieldForWriteLocksTheSpecimenAndChecksOwnership():
    field = _field(1)
    db = FakeDb(field=field)
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=SPECIMEN_ID)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=field)),
        MagicMock(scalar_one=MagicMock(return_value=db.result)),
    ])
    loadedField, loadedResult, specimen = await loadFieldForWrite(db, field.fieldId, MEDTECH_ID)
    assert (loadedField, loadedResult, specimen) == (field, db.result, db.specimen)
    assert db.lockedSpecimen


@pytest.mark.asyncio
async def test_loadFieldForWriteRefusesAnotherMedtechAndARejectedSpecimen():
    field = _field(1)
    for specimen, code in (
        (_specimen(medtechId=OTHER_MEDTECH_ID), "SPECIMEN_NOT_ASSIGNED"),
        (_specimen(status="REJECTED"), "SPECIMEN_REJECTED"),
    ):
        db = FakeDb(specimen=specimen)
        db.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=SPECIMEN_ID)))
        with pytest.raises(HTTPException) as excInfo:
            await loadFieldForWrite(db, field.fieldId, MEDTECH_ID)
        assert excInfo.value.errorCode == code


@pytest.mark.asyncio
async def test_loadFieldForWriteReports404ForAMissingField():
    db = FakeDb()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    with pytest.raises(NotFoundException) as excInfo:
        await loadFieldForWrite(db, uuid.uuid4(), MEDTECH_ID)
    assert excInfo.value.errorCode == "FIELD_NOT_FOUND"


@pytest.mark.parametrize("status", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION])
def test_verificationPhaseAllowsTheTwoEditableStatuses(status):
    requireVerificationPhase(_result(status))


@pytest.mark.parametrize("status", [
    ResultStatus.CAPTURING, ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED,
    ResultStatus.RELEASED, ResultStatus.CRITICAL_ESCALATED,
])
def test_verificationPhaseRefusesEveryOtherStatus(status):
    with pytest.raises(Exception) as excInfo:
        requireVerificationPhase(_result(status))
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"


def test_onlyAnAcceptedFieldMayBeCorrected():
    requireFieldAccepted(_field(1))
    for status in (FieldStatus.ANALYZED, FieldStatus.DISCARDED):
        with pytest.raises(Exception) as excInfo:
            requireFieldAccepted(_field(1, status=status))
        assert excInfo.value.errorCode == "INVALID_FIELD_STATUS"


# ── listFields ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_listFieldsReturnsEveryFieldInCaptureOrderWithCounts():
    fields = [
        _field(1, verifiedBy=MEDTECH_ID),
        _field(2),
        _field(3, status=FieldStatus.DISCARDED),
        _field(4, status=FieldStatus.ANALYZED),
    ]
    db = FakeDb(fields=fields)
    with _signed({"key-1": "u1", "key-2": "u2", "key-3": "u3", "key-4": "u4"}):
        response = await _service(db).listFields(RESULT_ID, MEDTECH)

    assert isinstance(response, FieldListResponse)
    assert [i.fieldNo for i in response.items] == [1, 2, 3, 4]
    assert [i.imageUrl for i in response.items] == ["u1", "u2", "u3", "u4"]
    assert response.acceptedFieldCount == 2
    assert response.verifiedFieldCount == 1
    assert response.undecidedFieldCount == 1
    assert response.minFields == settings.minFieldsPerSpecimen
    assert response.resultStatus == ResultStatus.PENDING_CONFIRM


@pytest.mark.asyncio
async def test_listFieldsNeverLoadsDetections():
    db = FakeDb(fields=[_field(1)])
    with _signed():
        await _service(db).listFields(RESULT_ID, MEDTECH)
    assert not any("detections" in s for s in db.statements)


@pytest.mark.asyncio
async def test_listFieldsSignsEveryImageInOneBatchedCall():
    db = FakeDb(fields=[_field(1), _field(2), _field(3)])
    sign = AsyncMock(return_value={})
    with patch("src.services.result_field_service.signedImageUrls", sign):
        await _service(db).listFields(RESULT_ID, MEDTECH)
    sign.assert_awaited_once_with(["key-1", "key-2", "key-3"])


@pytest.mark.asyncio
async def test_listFieldsRunsAFixedNumberOfQueriesWhateverTheFieldCount():
    small = FakeDb(fields=[_field(1)])
    large = FakeDb(fields=[_field(n) for n in range(1, 13)])
    with _signed():
        await _service(small).listFields(RESULT_ID, MEDTECH)
        await _service(large).listFields(RESULT_ID, MEDTECH)
    assert len(small.statements) == len(large.statements) == 3


@pytest.mark.asyncio
async def test_listFieldsFlagsAreComputedAgainstTheAcceptedFieldMeans():
    quiet = [_field(n, effective={"erythrocytes": 2}) for n in range(1, 11)]
    loud = _field(11, effective={"erythrocytes": 30}, ai={"erythrocytes": 30},
                  conf={"erythrocytes": 0.2})
    db = FakeDb(fields=[*quiet, loud])
    with _signed(), patch("src.services.result_field_service.loadNormalMax",
                          return_value={"erythrocytes": 5.0}):
        response = await _service(db).listFields(RESULT_ID, MEDTECH)

    reasons = {(f.reason, f.particleType) for f in response.items[-1].flags}
    assert reasons == {
        ("LOW_CONFIDENCE", None), ("OUTLIER_COUNT", "erythrocytes"),
        ("ABOVE_NORMAL_MAX", "erythrocytes"),
    }
    assert all(not item.flags for item in response.items[:10])


@pytest.mark.asyncio
async def test_aDiscardedFieldIsNeverFlaggedAndIsLeftOutOfTheMeans():
    fields = [_field(1, effective={"erythrocytes": 2}),
              _field(2, effective={"erythrocytes": 90}, status=FieldStatus.DISCARDED)]
    db = FakeDb(fields=fields)
    with _signed():
        response = await _service(db).listFields(RESULT_ID, MEDTECH)
    assert response.items[1].flags == []
    assert response.items[0].flags == []


@pytest.mark.asyncio
async def test_listFieldsReportsCorrectionsAndOverriddenClasses():
    edited, plain = _field(1), _field(2)
    overrideRow = ManualOverride(
        overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=plain.fieldId, medtechId=MEDTECH_ID,
        parameterName="crystals", originalAiValue="1.0", correctedValue="4.0", rationale="r",
        kind="OVERRIDE", overriddenAt=NOW)
    edit = FieldAnnotationEdit(
        editId=uuid.uuid4(), fieldId=edited.fieldId, action=EditAction.REMOVED, detectionId="d1",
        particleType="erythrocytes", editedBy=MEDTECH_ID, editedAt=NOW)
    db = FakeDb(fields=[edited, plain], edits=[edit], overrides=[overrideRow])
    with _signed():
        response = await _service(db).listFields(RESULT_ID, MEDTECH)

    first, second = response.items
    assert (first.hasCorrections, first.overriddenClasses) == (True, [])
    assert (second.hasCorrections, second.overriddenClasses) == (True, ["crystals"])


@pytest.mark.asyncio
async def test_aRevertedOverrideNoLongerCounts():
    field = _field(1)
    rows = [
        ManualOverride(overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=field.fieldId,
                       medtechId=MEDTECH_ID, parameterName="crystals", originalAiValue="1",
                       correctedValue="4", rationale="r", kind="OVERRIDE", overriddenAt=NOW),
        ManualOverride(overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=field.fieldId,
                       medtechId=MEDTECH_ID, parameterName="crystals", originalAiValue="1",
                       correctedValue="1", rationale="r", kind="REVERT",
                       overriddenAt=NOW + timedelta(minutes=1)),
    ]
    db = FakeDb(fields=[field], overrides=rows)
    with _signed():
        response = await _service(db).listFields(RESULT_ID, MEDTECH)
    assert response.items[0].overriddenClasses == []
    assert response.items[0].hasCorrections is False


@pytest.mark.asyncio
async def test_listFieldsIsForbiddenForAnotherMedtechButOpenToASupervisor():
    db = FakeDb(fields=[_field(1)])
    with _signed():
        with pytest.raises(ForbiddenException):
            await _service(db).listFields(RESULT_ID, OTHER_MEDTECH)
        assert (await _service(db).listFields(RESULT_ID, SUPERVISOR)).items


@pytest.mark.asyncio
async def test_listFieldsForAMissingResultIs404():
    db = FakeDb()
    db.result = None
    with pytest.raises(NotFoundException) as excInfo:
        await _service(db).listFields(RESULT_ID, SUPERVISOR)
    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_aResultWithNoFieldsListsNothing():
    db = FakeDb(fields=[])
    with _signed():
        response = await _service(db).listFields(RESULT_ID, MEDTECH)
    assert response.items == [] and response.acceptedFieldCount == 0


# ── getField ─────────────────────────────────────────────────────────────────

def _detections():
    return [{"id": "d1", "particleType": "erythrocytes", "confidence": 0.9,
             "x": 10.0, "y": 20.0, "w": 5.0, "h": 5.0}]


@pytest.mark.asyncio
async def test_getFieldReturnsDetectionsEditsAndOverridesInForce():
    field = _field(1, detections=_detections())
    edits = [
        FieldAnnotationEdit(editId=uuid.uuid4(), fieldId=field.fieldId, action=EditAction.REMOVED,
                            detectionId="d1", particleType="erythrocytes",
                            editedBy=MEDTECH_ID, editedAt=NOW),
        FieldAnnotationEdit(editId=uuid.uuid4(), fieldId=field.fieldId, action=EditAction.ADDED,
                            particleType="yeast", x=1.0, y=2.0, w=3.0, h=4.0,
                            editedBy=MEDTECH_ID, editedAt=NOW + timedelta(seconds=5)),
    ]
    override = ManualOverride(
        overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=field.fieldId, medtechId=MEDTECH_ID,
        parameterName="crystals", originalAiValue="1.0", correctedValue="6.0",
        rationale="Recounted", kind="OVERRIDE", overriddenAt=NOW)
    db = FakeDb(field=field, fields=[field], edits=edits, overrides=[override])

    with _signed({"key-detail": "https://signed.example/field-1"}):
        detail = await _service(db).getField(field.fieldId, MEDTECH, MagicMock())

    assert isinstance(detail, FieldDetail)
    assert [d.id for d in detail.detections] == ["d1"]
    assert (detail.imageWidth, detail.imageHeight) == (800, 600)
    assert detail.annotationEdits.removedDetectionIds == ["d1"]
    assert [(b.particleType, b.x, b.w) for b in detail.annotationEdits.addedBoxes] == [("yeast", 1.0, 3.0)]
    assert detail.annotationEdits.editedBy == MEDTECH_ID
    assert [(o.particleType, o.originalAiValue, o.correctedValue, o.rationale) for o in detail.overrides] == [
        ("crystals", 1, 6, "Recounted")]
    assert detail.overriddenClasses == ["crystals"] and detail.hasCorrections
    assert detail.imageUrl == "https://signed.example/field-1"


@pytest.mark.asyncio
async def test_getFieldAsksOnlyForTheCurrentEditsAndLoadsDetectionsExplicitly():
    field = _field(1, detections=_detections())
    db = FakeDb(field=field, fields=[field])
    with _signed():
        await _service(db).getField(field.fieldId, MEDTECH, MagicMock())
    assert any("superseded_at IS NULL" in s for s in db.statements)
    assert "detections" in db.statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION])
async def test_aMedtechOpeningAFieldInVerificationRecordsTheOpen(status):
    field = _field(1)
    db = FakeDb(result=_result(status), field=field, fields=[field])
    with _signed():
        await _service(db).getField(field.fieldId, MEDTECH, MagicMock())

    updates = _updates(db)
    assert len(updates) == 1
    assert "last_opened_by" in updates[0] and "last_opened_at" in updates[0]
    db.commit.assert_awaited_once()
    assert db.lockedSpecimen


@pytest.mark.asyncio
async def test_theOpenDoesNotBumpUpdatedAt():
    field = _field(1)
    db = FakeDb(field=field, fields=[field])
    with _signed():
        await _service(db).getField(field.fieldId, MEDTECH, MagicMock())
    update = _updates(db)[0]
    # `updated_at` is assigned to itself, so the column's onupdate does not fire.
    assert re.search(r"updated_at\s*=\s*result_fields\.updated_at", update)
    assert not re.search(r"updated_at\s*=\s*now\(\)", update, re.IGNORECASE)


@pytest.mark.asyncio
async def test_noOpenIsRecordedWhileTheResultIsCapturing():
    field = _field(1)
    db = FakeDb(result=_result(ResultStatus.CAPTURING), field=field, fields=[field])
    with _signed():
        detail = await _service(db).getField(field.fieldId, MEDTECH, MagicMock())
    assert detail.fieldId == field.fieldId
    assert _updates(db) == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_noOpenIsRecordedForASupervisor():
    field = _field(1)
    db = FakeDb(field=field, fields=[field])
    with _signed():
        await _service(db).getField(field.fieldId, SUPERVISOR, MagicMock())
    assert _updates(db) == []
    db.commit.assert_not_awaited()
    assert not db.lockedSpecimen


@pytest.mark.asyncio
async def test_noOpenIsRecordedIfTheStatusChangedWhileTakingTheLock():
    field = _field(1)
    db = FakeDb(field=field, fields=[field], statusAfterLock=ResultStatus.CAPTURING)
    with _signed():
        await _service(db).getField(field.fieldId, MEDTECH, MagicMock())
    assert _updates(db) == []


@pytest.mark.asyncio
async def test_aMedtechCannotOpenAnotherMedtechsFieldAndNothingIsWritten():
    field = _field(1)
    db = FakeDb(field=field, fields=[field])
    with _signed(), pytest.raises(ForbiddenException):
        await _service(db).getField(field.fieldId, OTHER_MEDTECH, MagicMock())
    assert _updates(db) == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_getFieldForAMissingFieldIs404():
    db = FakeDb(field=None)
    with pytest.raises(NotFoundException) as excInfo:
        await _service(db).getField(uuid.uuid4(), MEDTECH, MagicMock())
    assert excInfo.value.errorCode == "FIELD_NOT_FOUND"


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def routeClient():
    app.dependency_overrides[getCurrentUser] = lambda: MEDTECH
    yield TestClient(app)
    for dependency in (getCurrentUser, getResultFieldService, getConfirmationService):
        app.dependency_overrides.pop(dependency, None)


def test_listAndDetailAreNotFoundWhileTheFlagIsOff(routeClient, monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    assert routeClient.get(f"/api/v1/results/{RESULT_ID}/fields").status_code == 404
    assert routeClient.get(f"/api/v1/fields/{uuid.uuid4()}").status_code == 404


def test_listAndDetailCallTheServiceWhenTheFlagIsOn(routeClient, monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    listing = FieldListResponse(
        resultId=RESULT_ID, resultStatus=ResultStatus.PENDING_CONFIRM, minFields=10,
        acceptedFieldCount=0, verifiedFieldCount=0, undecidedFieldCount=0, items=[])
    service = MagicMock(listFields=AsyncMock(return_value=listing))
    app.dependency_overrides[getResultFieldService] = lambda: service

    response = routeClient.get(f"/api/v1/results/{RESULT_ID}/fields")

    assert response.status_code == 200
    assert response.json()["minFields"] == 10
    service.listFields.assert_awaited_once()
    assert service.listFields.await_args.args[0] == RESULT_ID


def test_otherFieldRoutesStillAnswer501(routeClient, monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    assert routeClient.post(f"/api/v1/fields/{uuid.uuid4()}/accept").status_code == 501


# ── storage: signedImageUrls ─────────────────────────────────────────────────

def _supabase(items=None, error: Exception | None = None):
    bucket = MagicMock()
    bucket.create_signed_urls = AsyncMock(return_value=items, side_effect=error)
    client = MagicMock()
    client.storage.from_.return_value = bucket
    return client, bucket


@pytest.mark.asyncio
async def test_signedImageUrlsSignsEveryKeyInOneCallWithTheConfiguredTtl():
    client, bucket = _supabase([
        {"path": "a.jpg", "signedURL": "https://s/a", "error": None},
        {"path": "b.jpg", "signedURL": "https://s/b", "error": None},
    ])
    with patch.object(storage, "supabase", client):
        urls = await storage.signedImageUrls(["a.jpg", "b.jpg", "a.jpg", None, ""])

    assert urls == {"a.jpg": "https://s/a", "b.jpg": "https://s/b"}
    bucket.create_signed_urls.assert_awaited_once_with(["a.jpg", "b.jpg"], storage.SIGNED_IMAGE_URL_TTL_SECONDS)
    client.storage.from_.assert_called_once_with(settings.supabaseImageBucket)


@pytest.mark.asyncio
async def test_signedImageUrlsMapsAKeyThatFailedToNone():
    client, _ = _supabase([
        {"path": "a.jpg", "signedURL": "https://s/a", "error": None},
        {"path": "b.jpg", "signedURL": None, "error": "Object not found"},
    ])
    with patch.object(storage, "supabase", client):
        urls = await storage.signedImageUrls(["a.jpg", "b.jpg"])
    assert urls == {"a.jpg": "https://s/a", "b.jpg": None}


@pytest.mark.asyncio
async def test_signedImageUrlsNeverRaisesAndNeverLogsAUrl(caplog):
    client, _ = _supabase(error=RuntimeError("boom"))
    with patch.object(storage, "supabase", client), caplog.at_level("DEBUG"):
        urls = await storage.signedImageUrls(["a.jpg", "b.jpg"])
    assert urls == {"a.jpg": None, "b.jpg": None}
    assert "token=" not in caplog.text


@pytest.mark.asyncio
async def test_signedImageUrlsLogsNoUrlOnSuccess(caplog):
    client, _ = _supabase([{"path": "a.jpg", "signedURL": "https://s/a?token=SECRET", "error": None}])
    with patch.object(storage, "supabase", client), caplog.at_level("DEBUG"):
        await storage.signedImageUrls(["a.jpg"])
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [[], [None], [""], [None, ""]])
async def test_signedImageUrlsMakesNoCallWithoutKeys(keys):
    client, bucket = _supabase([])
    with patch.object(storage, "supabase", client):
        assert await storage.signedImageUrls(keys) == {}
    bucket.create_signed_urls.assert_not_awaited()


@pytest.mark.asyncio
async def test_theSingleUrlHelperIsUnchanged():
    bucket = MagicMock(create_signed_url=AsyncMock(return_value={"signedUrl": "https://s/one"}))
    client = MagicMock()
    client.storage.from_.return_value = bucket
    with patch.object(storage, "supabase", client):
        assert await storage.signedImageUrl("one.jpg") == "https://s/one"
    bucket.create_signed_url.assert_awaited_once_with("one.jpg", storage.SIGNED_IMAGE_URL_TTL_SECONDS)


# ── GET /results/medtech/pending: field counts ───────────────────────────────

def _listing():
    return {"items": [
        {"resultId": uuid.UUID("00000000-0000-0000-0000-0000000004a1"),
         "specimenId": uuid.UUID("00000000-0000-0000-0000-0000000004a2"),
         "sampleUid": "SMP-20261027-00001", "testType": "Urinalysis", "priorityLevel": "ROUTINE",
         "receivedAt": datetime(2026, 10, 27, 1, 2, 3, tzinfo=UTC), "patientUid": "PAT-0001",
         "patientAge": 41, "patientSex": "F", "status": "PENDING_CONFIRM", "returnReason": None},
        {"resultId": uuid.UUID("00000000-0000-0000-0000-0000000004a4"),
         "specimenId": uuid.UUID("00000000-0000-0000-0000-0000000004a5"),
         "sampleUid": None, "testType": None, "priorityLevel": None, "receivedAt": None,
         "patientUid": "", "patientAge": None, "patientSex": None,
         "status": "RETURNED_FOR_CORRECTION", "returnReason": "Blurry image"},
    ], "total": 2, "page": 1, "pageSize": 20}


def _pendingClient(listing):
    service = MagicMock(listPendingForMedtech=AsyncMock(return_value=listing))
    app.dependency_overrides[getCurrentUser] = lambda: MEDTECH
    app.dependency_overrides[getConfirmationService] = lambda: service
    return TestClient(app)


def test_pendingJsonWithTheFlagOffIsByteIdenticalToTheGolden(monkeypatch, routeClient):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    response = _pendingClient(_listing()).get("/api/v1/results/medtech/pending")

    assert response.status_code == 200
    assert response.text == GOLDEN.read_text(encoding="utf-8").rstrip("\n")


def test_theGoldenFileHasNoFieldCountKeys():
    text = GOLDEN.read_text(encoding="utf-8")
    for key in ("fieldCount", "verifiedFieldCount", "flaggedFieldCount"):
        assert key not in text


def test_pendingItemsCarryTheCountsWhenTheServiceSuppliesThem(monkeypatch, routeClient):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    listing = _listing()
    listing["items"][0].update(fieldCount=12, verifiedFieldCount=10, flaggedFieldCount=2)
    listing["items"][1].update(fieldCount=0, verifiedFieldCount=0, flaggedFieldCount=0)

    items = _pendingClient(listing).get("/api/v1/results/medtech/pending").json()["items"]

    assert (items[0]["fieldCount"], items[0]["verifiedFieldCount"], items[0]["flaggedFieldCount"]) == (12, 10, 2)
    assert (items[1]["fieldCount"], items[1]["verifiedFieldCount"], items[1]["flaggedFieldCount"]) == (0, 0, 0)


def test_pendingCountsAreOmittedNotNullWhenNone(routeClient):
    items = _pendingClient(_listing()).get("/api/v1/results/medtech/pending").json()["items"]
    assert all("fieldCount" not in item for item in items)


def test_theOpenapiSchemaStillDescribesTheCountFields(routeClient):
    schema = TestClient(app).get("/openapi.json").json()["components"]["schemas"]["MedtechPendingResultItem"]
    assert {"fieldCount", "verifiedFieldCount", "flaggedFieldCount", "returnReason"} <= set(schema["properties"])


def _pendingRow(status="PENDING_CONFIRM"):
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = uuid.uuid4()
    ar.specimenId = uuid.uuid4()
    ar.status = status
    spec = MagicMock(spec=Specimen)
    spec.specimenId = ar.specimenId
    spec.sampleUid = "SMP-1"
    spec.testType = "Urinalysis"
    spec.priorityLevel = "ROUTINE"
    spec.receivedAt = NOW
    spec.patientUid = "PAT-000001"
    return ar, spec


def _fieldRow(resultId, verified=False, effective=None, conf=None):
    row = MagicMock()
    row.resultId = resultId
    row.verifiedBy = MEDTECH_ID if verified else None
    row.aiFindings = effective or {"erythrocytes": 4}
    row.effectiveCounts = effective or {"erythrocytes": 4}
    row.confidenceScores = conf or {}
    return row


def _pendingDb(rows, fieldRows=None, withFields=False):
    patient = MagicMock(spec=Patient)
    patient.patientUid = "PAT-000001"
    patient.dateOfBirth = None
    patient.sex = "F"
    effects = [
        MagicMock(scalar_one=MagicMock(return_value=len(rows))),
        MagicMock(all=MagicMock(return_value=rows)),
    ]
    if withFields:
        effects.append(MagicMock(all=MagicMock(return_value=fieldRows or [])))
    patients = MagicMock()
    patients.scalars.return_value.all.return_value = [patient]
    effects.append(patients)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=effects)
    return db


def _confirmationService(db):
    return ResultConfirmationService(
        db=db, auditLogger=MagicMock(record=AsyncMock()), _smartDiagnosisService=MagicMock(),
        _notifService=MagicMock())


@pytest.mark.asyncio
async def test_withTheFlagOffThePendingQueryAddsNothing(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    db = _pendingDb([_pendingRow()])

    listing = await _confirmationService(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    assert db.execute.await_count == 3  # count, rows, patients: no field query
    assert not any(k in listing["items"][0] for k in ("fieldCount", "verifiedFieldCount", "flaggedFieldCount"))


@pytest.mark.asyncio
async def test_withTheFlagOnThePendingQueryAddsExactlyOneGroupedQuery(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    rowsSmall = [_pendingRow()]
    rowsLarge = [_pendingRow() for _ in range(7)]

    dbSmall = _pendingDb(rowsSmall, withFields=True)
    dbLarge = _pendingDb(rowsLarge, withFields=True)
    await _confirmationService(dbSmall)._queryPendingForMedtech(MEDTECH_ID, 1, 20)
    await _confirmationService(dbLarge)._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    assert dbSmall.execute.await_count == dbLarge.execute.await_count == 4


@pytest.mark.asyncio
async def test_pendingCountsAreAcceptedVerifiedAndFlagged(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    (arA, specA), (arB, specB) = _pendingRow(), _pendingRow()
    fieldRows = [
        _fieldRow(arA.resultId, verified=True),
        _fieldRow(arA.resultId, verified=True),
        _fieldRow(arA.resultId, verified=False, effective={"erythrocytes": 40}, conf={"erythrocytes": 0.9}),
        _fieldRow(arA.resultId, verified=False, conf={"erythrocytes": 0.1}),  # low confidence
    ]
    db = _pendingDb([(arA, specA), (arB, specB)], fieldRows, withFields=True)

    with patch("src.services.result_confirmation_service.flagThresholds",
               return_value=__import__("src.services.field_aggregation_service", fromlist=["x"]).FlagThresholds(
                   lowConfidence=0.5, outlierMultiplier=2.0, outlierMinAbove=5, normalMax={})):
        listing = await _confirmationService(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    first, second = listing["items"]
    assert (first["fieldCount"], first["verifiedFieldCount"], first["flaggedFieldCount"]) == (4, 2, 2)
    assert (second["fieldCount"], second["verifiedFieldCount"], second["flaggedFieldCount"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_thePendingFieldQueryNeverSelectsDetections(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = _pendingDb([_pendingRow()], withFields=True)
    await _confirmationService(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20)
    fieldQuery = str(db.execute.await_args_list[2].args[0])
    assert "result_fields" in fieldQuery and "detections" not in fieldQuery


def test_theGoldenFileIsValidJson():
    assert json.loads(GOLDEN.read_text(encoding="utf-8"))["total"] == 2
