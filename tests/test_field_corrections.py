"""Unit tests — multi-field corrections (web lane, W2).

Box edits, per-field overrides and reverts, marking a field as checked, the web
path of discard (the capturing path is in test_field_capture_decisions.py), and
reopen-capture. The session is a small stateful fake that
routes `execute` by the compiled SQL, so the services run end to end without a
database.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app, httpExceptionHandler
from src.api.fields import (
    getFieldCorrectionService,
    getFieldOverrideService,
    getResultFieldService,
)
from src.core.config import settings
from src.core.exceptions import OverrideInForceError
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.field_annotation_edit import EditAction, FieldAnnotationEdit
from src.models.image import Image, ImageStatus
from src.models.manual_override import ManualOverride, OverrideKind
from src.models.result_field import FieldStatus, ResultField
from src.models.specimen import Specimen
from src.schemas.field import (
    AddedBoxInput,
    FieldOverrideRequest,
    RevertOverrideRequest,
    SaveAnnotationsRequest,
)
from src.services.field_correction_service import FieldCorrectionService
from src.services.manual_override_service import ManualOverrideService
from src.services.notification_service import NotificationService
from src.services.result_field_service import ResultFieldService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000008a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000008a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000008a3")
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000008a4")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)

DETECTIONS = [
    {"id": "d1", "particleType": "erythrocytes", "confidence": 0.9, "x": 10.0, "y": 10.0, "w": 5.0, "h": 5.0},
    {"id": "d2", "particleType": "erythrocytes", "confidence": 0.9, "x": 20.0, "y": 10.0, "w": 5.0, "h": 5.0},
    {"id": "d3", "particleType": "erythrocytes", "confidence": 0.8, "x": 30.0, "y": 10.0, "w": 5.0, "h": 5.0},
    {"id": "d4", "particleType": "crystals", "confidence": 0.7, "x": 40.0, "y": 10.0, "w": 5.0, "h": 5.0},
]


def mkField(n=1, *, status=FieldStatus.ACCEPTED, ai=None, effective=None, detections=None, **kw) -> ResultField:
    ai = ai if ai is not None else {"erythrocytes": 9, "crystals": 1}
    return ResultField(
        fieldId=kw.pop("fieldId", uuid.UUID(f"00000000-0000-0000-0000-0000000009{n:02d}")),
        resultId=RESULT_ID, specimenId=SPECIMEN_ID,
        imageId=uuid.UUID(f"00000000-0000-0000-0000-000000000a{n:02d}"),
        fieldNo=n, magnification="HPF", status=status,
        aiFindings=ai, detections=[dict(d) for d in (detections if detections is not None else DETECTIONS)],
        imageWidth=800, imageHeight=600, confidenceScores={"erythrocytes": 0.9},
        effectiveCounts=dict(effective if effective is not None else ai),
        smartDiagnosis=None, smartDiagnosisUnavailable=False, modelVersion="mvp-v1.0",
        discardReason=None, verifiedBy=kw.pop("verifiedBy", None), verifiedAt=kw.pop("verifiedAt", None),
        lastOpenedBy=kw.pop("lastOpenedBy", None), lastOpenedAt=kw.pop("lastOpenedAt", None),
        lastCorrectedAt=kw.pop("lastCorrectedAt", None),
        version=kw.pop("version", 1), createdAt=NOW - timedelta(hours=1), updatedAt=NOW, **kw,
    )


def mkResult(status=ResultStatus.PENDING_CONFIRM, **kw) -> AnalysisResult:
    return AnalysisResult(
        resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=status, fieldCount=kw.pop("fieldCount", 1),
        isLegacySingleField=kw.pop("isLegacySingleField", False), imageId=kw.pop("imageId", None), **kw,
    )


def mkSpecimen(medtechId=MEDTECH_ID, status="ASSIGNED") -> Specimen:
    return Specimen(specimenId=SPECIMEN_ID, medtechId=medtechId, status=status, sampleUid="SMP-20261027-00007")


class CorrectionDb:
    """Stateful fake `AsyncSession`: routes `execute` by compiled SQL and keeps edits/overrides."""

    def __init__(self, field, *, result=None, specimen=None, fields=None, edits=(), overrides=()):
        self.field = field
        self.fields = list(fields) if fields is not None else ([field] if field else [])
        self.result = result if result is not None else mkResult()
        self.specimen = specimen if specimen is not None else mkSpecimen()
        self.edits = list(edits)
        self.overrides = list(overrides)
        self.images = {f.imageId: Image(imageId=f.imageId, specimenId=SPECIMEN_ID, status=ImageStatus.ACTIVE)
                       for f in self.fields}
        self.statements: list[str] = []
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.lockedSpecimen = False

    # session API ---------------------------------------------------------
    async def get(self, model, key, **kwargs):
        if model is AnalysisResult:
            return self.result
        if model is Specimen:
            self.lockedSpecimen = self.lockedSpecimen or bool(kwargs.get("with_for_update"))
            return self.specimen
        if model is Image:
            return self.images.get(key)
        return None

    async def refresh(self, obj, **kwargs):
        if isinstance(obj, AnalysisResult) and obj.updatedAt is None:
            obj.updatedAt = NOW

    def add(self, obj):
        if isinstance(obj, FieldAnnotationEdit):
            self.edits.append(obj)
        elif isinstance(obj, ManualOverride):
            self.overrides.append(obj)

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        if low.startswith("update field_annotation_edits"):
            for edit in self.edits:
                if edit.supersededAt is None:
                    edit.supersededAt = NOW
        elif low.startswith("select result_fields.specimen_id from"):
            out.scalar_one_or_none.return_value = self.field.specimenId if self.field else None
        elif low.startswith("select result_fields.result_id from"):
            out.scalar_one_or_none.return_value = self.field.resultId if self.field else None
        elif low.startswith("select result_fields.image_id from"):
            ordered = sorted(self.fields, key=lambda f: f.fieldNo)
            out.scalars.return_value.all.return_value = [
                f.imageId for f in ordered if f.status == FieldStatus.ACCEPTED]
        elif "from result_fields join images" in low:
            out.all.return_value = [(f, f"key-{f.fieldNo}") for f in sorted(self.fields, key=lambda f: f.fieldNo)]
        elif "from result_fields" in low:
            out.scalar_one_or_none.return_value = self.field
        elif low.startswith("select analysis_results.result_id"):
            out.scalar_one.return_value = self.result
        elif "from field_annotation_edits" in low and "limit" in low:
            current = [e for e in self.edits if e.supersededAt is None]
            out.first.return_value = (current[0].editId,) if current else None
        elif "from field_annotation_edits" in low:
            out.scalars.return_value.all.return_value = [e for e in self.edits if e.supersededAt is None]
        elif "from manual_overrides" in low:
            out.scalars.return_value.all.return_value = sorted(self.overrides, key=lambda o: o.overriddenAt)
        else:  # pragma: no cover - a query the fake does not know about
            raise AssertionError(f"unexpected statement: {low}")
        return out

    # helpers -------------------------------------------------------------
    @property
    def currentEdits(self):
        return [e for e in self.edits if e.supersededAt is None]


@pytest.fixture(autouse=True)
def noStorage():
    with patch("src.services.result_field_service.signedImageUrls", AsyncMock(return_value={})):
        yield


def audit():
    return MagicMock(record=AsyncMock())


def corrections(db):
    return FieldCorrectionService(db=db, auditLogger=audit())


def overrides(db):
    return ManualOverrideService(db=db, auditLogger=audit())


def body(removed=(), added=(), version=1) -> SaveAnnotationsRequest:
    return SaveAnnotationsRequest(
        removedDetectionIds=list(removed), addedBoxes=list(added), expectedVersion=version)


def box(particleType="yeast", x=1.0, y=2.0, w=3.0, h=4.0) -> AddedBoxInput:
    return AddedBoxInput(particleType=particleType, x=x, y=y, w=w, h=h)


def assertCorrected(field: ResultField, versionBefore: int, who=MEDTECH_ID):
    """R4 + Q6: version up, corrected and opened at the same instant by the caller, mark cleared."""
    assert field.version == versionBefore + 1
    assert field.lastCorrectedAt is not None
    assert field.lastOpenedAt == field.lastCorrectedAt
    assert field.lastOpenedBy == who
    assert field.verifiedBy is None and field.verifiedAt is None


def assertNothingWritten(db: CorrectionDb, service):
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()
    assert not any(s.lower().startswith("update field_annotation_edits") for s in db.statements)


# ── saveAnnotations ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aStaleVersionWritesNothing():
    field = mkField(version=2)
    db = CorrectionDb(field)
    service = corrections(db)

    with pytest.raises(HTTPException) as excInfo:
        await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"], version=1), MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELD_VERSION_CONFLICT")
    assertNothingWritten(db, service)
    assert db.edits == [] and field.version == 2


@pytest.mark.asyncio
async def test_removingThreeBoxesGivesTheProposalExample():
    field = mkField(ai={"erythrocytes": 9}, verifiedBy=MEDTECH_ID, verifiedAt=NOW)
    db = CorrectionDb(field)
    service = corrections(db)

    response = await service.saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1", "d2", "d3"]), MagicMock())

    assert field.effectiveCounts["erythrocytes"] == 6
    assert response.field.effectiveCounts["erythrocytes"] == 6
    assert response.field.hasCorrections and response.field.version == 2
    assertCorrected(field, 1)
    assert [(e.action, e.detectionId, e.particleType) for e in db.currentEdits] == [
        (EditAction.REMOVED, "d1", "erythrocytes"), (EditAction.REMOVED, "d2", "erythrocytes"),
        (EditAction.REMOVED, "d3", "erythrocytes")]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_addedBoxesAreStoredAsPercentAndCounted():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)

    await corrections(db).saveAnnotations(
        field.fieldId, MEDTECH_ID, body(added=[box("yeast", 10.5, 20.25, 5, 5), box("yeast")]), MagicMock())

    assert field.effectiveCounts["yeast"] == 2
    added = [e for e in db.currentEdits if e.action == EditAction.ADDED]
    assert [(e.particleType, e.x, e.y, e.w, e.h) for e in added][0] == ("yeast", 10.5, 20.25, 5, 5)
    assert all(e.editedBy == MEDTECH_ID for e in added)


@pytest.mark.asyncio
async def test_aSaveSupersedesTheOldRowsAndNeverDeletesThem():
    old = FieldAnnotationEdit(
        editId=uuid.uuid4(), fieldId=mkField().fieldId, action=EditAction.REMOVED, detectionId="d1",
        particleType="erythrocytes", editedBy=MEDTECH_ID, editedAt=NOW - timedelta(minutes=5))
    field = mkField(ai={"erythrocytes": 9})
    old.fieldId = field.fieldId
    db = CorrectionDb(field, edits=[old])

    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d2"]), MagicMock())

    assert old in db.edits and old.supersededAt is not None
    assert [e.detectionId for e in db.currentEdits] == ["d2"]
    assert len(db.edits) == 2
    assert not any(s.lower().startswith("delete") for s in db.statements)


@pytest.mark.asyncio
async def test_theSavedVersionIsRequiredNextTime():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    service = corrections(db)
    await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"], version=1), MagicMock())

    with pytest.raises(HTTPException) as excInfo:
        await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1", "d2"], version=1), MagicMock())
    assert excInfo.value.errorCode == "FIELD_VERSION_CONFLICT"

    await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1", "d2"], version=2), MagicMock())
    assert field.version == 3


@pytest.mark.asyncio
async def test_theAiOutputIsNeverEdited():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    before = (dict(field.aiFindings), [dict(d) for d in field.detections], dict(field.confidenceScores))
    db = CorrectionDb(field)

    await corrections(db).saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1", "d4"], added=[box()]), MagicMock())
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 2, "r", MEDTECH_ID, MagicMock())

    assert (field.aiFindings, field.detections, field.confidenceScores) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"removed": ["nope"]}, "DETECTION_NOT_FOUND"),
        ({"removed": ["d1", "d1"]}, "DETECTION_NOT_FOUND"),
        ({"added": [box(x=95, w=10)]}, "BOX_OUT_OF_BOUNDS"),
        ({"added": [box(y=99.5, h=1.0)]}, "BOX_OUT_OF_BOUNDS"),
        ({"added": [box(x=0, y=0, w=1, h=1)] * 1001}, "TOO_MANY_BOXES"),
    ],
)
async def test_invalidBoxListsAreRefusedWithNothingWritten(kwargs, code):
    field = mkField()
    db = CorrectionDb(field)
    service = corrections(db)

    with pytest.raises(HTTPException) as excInfo:
        await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(**kwargs), MagicMock())

    assert (excInfo.value.status_code, excInfo.value.errorCode) == (422, code)
    assertNothingWritten(db, service)
    assert field.version == 1


@pytest.mark.asyncio
async def test_aBoxExactlyOnTheEdgeIsAccepted():
    field = mkField()
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(
        field.fieldId, MEDTECH_ID, body(added=[box(x=90, y=95, w=10, h=5)]), MagicMock())
    assert field.version == 2


@pytest.mark.asyncio
async def test_anIdenticalListIsANoOp():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    service = corrections(db)
    await service.saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1"], added=[box("yeast", 1.2345, 2.0, 3.0, 4.0)]), MagicMock())
    db.commit.reset_mock()
    service.auditLogger.record.reset_mock()
    stamp = (field.version, field.lastCorrectedAt)
    statementsBefore = len(db.statements)

    response = await service.saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1"], added=[box("yeast", 1.2345, 2.0, 3.0, 4.0)], version=2),
        MagicMock())

    assert (field.version, field.lastCorrectedAt) == stamp
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()
    assert not any(s.lower().startswith("update field_annotation_edits") for s in db.statements[statementsBefore:])
    assert response.field.version == 2 and len(db.edits) == 2


@pytest.mark.asyncio
async def test_aNoOpDoesNotClearTheVerifiedMark():
    field = mkField(verifiedBy=MEDTECH_ID, verifiedAt=NOW)
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(), MagicMock())
    assert field.verifiedBy == MEDTECH_ID and field.version == 1


@pytest.mark.asyncio
async def test_everyRealSaveWritesOneAuditRowWithCountsOnly():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    service = corrections(db)
    request = MagicMock()

    await service.saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1", "d2"], added=[box("yeast", 11.5, 12.5, 6, 7)]), request)

    service.auditLogger.record.assert_awaited_once()
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELD_ANNOTATIONS_SAVED"
    assert kwargs["entityType"] == "analysis_result" and kwargs["entityId"] == RESULT_ID
    assert kwargs["userId"] == MEDTECH_ID and kwargs["db"] is db and kwargs["request"] is request
    assert kwargs["detailJson"] == {
        "specimen_id": str(SPECIMEN_ID), "field_id": str(field.fieldId), "added": 1, "removed": 2}
    assert "11.5" not in str(kwargs) and "12.5" not in str(kwargs)


@pytest.mark.asyncio
async def test_aCorrectionClearsTheVerifiedMark():
    field = mkField(verifiedBy=MEDTECH_ID, verifiedAt=NOW)
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"]), MagicMock())
    assertCorrected(field, 1)


# ── ownership and phase guards (every correction route) ──────────────────────

def _calls(db, field):
    """One callable per correction service method, for the shared guard tests."""
    return {
        "save": lambda: corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"]), MagicMock()),
        "override": lambda: overrides(db).overrideFieldParameter(
            field.fieldId, "erythrocytes", 2, "r", MEDTECH_ID, MagicMock()),
        "revert": lambda: overrides(db).revertFieldOverride(
            field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock()),
        "verify": lambda: corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock()),
        "discard": lambda: ResultFieldService(db=db, auditLogger=audit()).discardField(
            field.fieldId, MEDTECH_ID, "reason", MagicMock()),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["save", "override", "revert", "verify", "discard"])
async def test_anotherMedtechIsForbiddenAndNothingIsWritten(name):
    field = mkField()
    db = CorrectionDb(field, specimen=mkSpecimen(medtechId=OTHER_ID))
    with pytest.raises(HTTPException) as excInfo:
        await _calls(db, field)[name]()
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (403, "SPECIMEN_NOT_ASSIGNED")
    db.commit.assert_not_awaited()
    assert db.edits == [] and db.overrides == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["save", "override", "revert", "verify", "discard"])
async def test_aRejectedSpecimenIsRefused(name):
    field = mkField()
    db = CorrectionDb(field, specimen=mkSpecimen(status="REJECTED"))
    with pytest.raises(HTTPException) as excInfo:
        await _calls(db, field)[name]()
    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["save", "override", "revert", "verify"])
@pytest.mark.parametrize("status", [
    ResultStatus.CAPTURING, ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED,
    ResultStatus.RELEASED, ResultStatus.CRITICAL_ESCALATED])
async def test_correctionsAreRefusedOutsideVerification(name, status):
    field = mkField()
    db = CorrectionDb(field, result=mkResult(status))
    with pytest.raises(HTTPException) as excInfo:
        await _calls(db, field)[name]()
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["save", "override", "revert", "verify"])
@pytest.mark.parametrize("status", [FieldStatus.ANALYZED, FieldStatus.DISCARDED])
async def test_correctionsApplyToAcceptedFieldsOnly(name, status):
    field = mkField(status=status)
    db = CorrectionDb(field)
    with pytest.raises(HTTPException) as excInfo:
        await _calls(db, field)[name]()
    assert excInfo.value.errorCode == "INVALID_FIELD_STATUS"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["save", "override", "revert", "verify", "discard"])
async def test_aMissingFieldIs404(name):
    field = mkField()
    db = CorrectionDb(None)
    db.field = None
    calls = _calls(CorrectionDb(field), field)  # build callables on a throwaway, then point at the empty db
    del calls
    missing = mkField(fieldId=uuid.uuid4())
    with pytest.raises(HTTPException) as excInfo:
        await _calls(db, missing)[name]()
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (404, "FIELD_NOT_FOUND")


@pytest.mark.asyncio
async def test_theSpecimenIsLockedBeforeAnyWrite():
    field = mkField()
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"]), MagicMock())
    assert db.lockedSpecimen


# ── overrides ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anOverrideIsStoredPerFieldAsTextFromTheFieldsAiCount():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    db = CorrectionDb(field)
    service = overrides(db)

    response = await service.overrideFieldParameter(
        field.fieldId, "erythrocytes", 4, "Recounted by eye", MEDTECH_ID, MagicMock())

    (row,) = db.overrides
    assert (row.fieldId, row.resultId, row.medtechId) == (field.fieldId, RESULT_ID, MEDTECH_ID)
    assert (row.parameterName, row.kind) == ("erythrocytes", OverrideKind.OVERRIDE)
    assert (row.originalAiValue, row.correctedValue, row.rationale) == ("9.0", "4.0", "Recounted by eye")
    assert field.effectiveCounts["erythrocytes"] == 4
    assert response.field.overriddenClasses == ["erythrocytes"]
    assertCorrected(field, 1)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_anOverrideOnAClassTheAiDidNotFindIsAllowed():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "yeast", 3, "Missed", MEDTECH_ID, MagicMock())
    (row,) = db.overrides
    assert (row.originalAiValue, row.correctedValue) == ("0.0", "3.0")
    assert field.effectiveCounts["yeast"] == 3


@pytest.mark.asyncio
async def test_anOverrideEqualToTheCurrentCountIsRefused():
    field = mkField(ai={"erythrocytes": 9}, effective={"erythrocytes": 6})
    db = CorrectionDb(field)
    service = overrides(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.overrideFieldParameter(field.fieldId, "erythrocytes", 6, "r", MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (422, "OVERRIDE_UNCHANGED")
    assert db.overrides == [] and field.version == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_theOverrideAuditRowNamesTheValuesButNotTheReason():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    service = overrides(db)

    await service.overrideFieldParameter(
        field.fieldId, "erythrocytes", 4, "Secret clinical reason", MEDTECH_ID, MagicMock())

    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "RESULT_OVERRIDDEN"
    assert kwargs["detailJson"] == {
        "specimen_id": str(SPECIMEN_ID), "field_id": str(field.fieldId), "parameter": "erythrocytes",
        "original_ai_value": 9.0, "corrected_value": 4}
    assert "Secret clinical reason" not in str(kwargs)


@pytest.mark.asyncio
async def test_anOverrideLocksBoxEditingForThatClass():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())
    service = corrections(db)

    with pytest.raises(OverrideInForceError):
        await service.saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"], version=2), MagicMock())
    with pytest.raises(OverrideInForceError):
        await service.saveAnnotations(
            field.fieldId, MEDTECH_ID, body(added=[box("erythrocytes")], version=2), MagicMock())

    assert field.version == 2 and db.edits == []
    service.auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_otherClassesCanStillBeEditedWhileOneIsOverridden():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())

    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d4"], version=2), MagicMock())

    assert field.effectiveCounts == {"erythrocytes": 4, "crystals": 0}


@pytest.mark.asyncio
async def test_resendingTheUnchangedEditsOfAnOverriddenClassIsAccepted():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"]), MagicMock())
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())

    # the editor sends the whole list: the d1 removal is unchanged, a crystals removal is new
    await corrections(db).saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1", "d4"], version=3), MagicMock())

    assert field.effectiveCounts["erythrocytes"] == 4 and field.effectiveCounts["crystals"] == 0
    assert field.version == 4


@pytest.mark.asyncio
async def test_aRevertRestoresTheBoxCountIncludingKeptEdits():
    field = mkField(ai={"erythrocytes": 9, "crystals": 1})
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(
        field.fieldId, MEDTECH_ID, body(removed=["d1", "d2"]), MagicMock())          # 9 - 2 = 7
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 2, "r", MEDTECH_ID, MagicMock())
    assert field.effectiveCounts["erythrocytes"] == 2
    service = overrides(db)

    response = await service.revertFieldOverride(
        field.fieldId, "erythrocytes", "Box count was right", MEDTECH_ID, MagicMock())

    assert field.effectiveCounts["erythrocytes"] == 7
    assert response.field.overriddenClasses == []
    revert = db.overrides[-1]
    assert (revert.kind, revert.fieldId, revert.parameterName) == (OverrideKind.REVERT, field.fieldId, "erythrocytes")
    assert (revert.originalAiValue, revert.correctedValue, revert.rationale) == ("9.0", "7.0", "Box count was right")
    assert len(db.overrides) == 2                        # nothing is deleted
    assertCorrected(field, 3)
    assert service.auditLogger.record.call_args.kwargs["eventType"] == "OVERRIDE_REVERTED"
    assert "Box count was right" not in str(service.auditLogger.record.call_args)


@pytest.mark.asyncio
async def test_aRevertUnlocksBoxEditing():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())
    await overrides(db).revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())

    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"], version=3), MagicMock())
    assert field.effectiveCounts["erythrocytes"] == 8


@pytest.mark.asyncio
async def test_revertingWithoutAnOverrideIsRefused():
    field = mkField()
    db = CorrectionDb(field)
    service = overrides(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "OVERRIDE_NOT_IN_FORCE")
    assert db.overrides == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_revertingTwiceIsRefusedTheSecondTime():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())
    await overrides(db).revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())
    with pytest.raises(HTTPException) as excInfo:
        await overrides(db).revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "OVERRIDE_NOT_IN_FORCE"


@pytest.mark.asyncio
async def test_aLaterOverrideAfterARevertCountsAgain():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    for step in range(3):
        # Earlier rows are moved well into the past so the row written next, stamped
        # with the real clock, is the latest ("latest decides" must not depend on timing).
        for index, row in enumerate(db.overrides):
            row.overriddenAt = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
        if step == 1:
            await overrides(db).revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())
        else:
            await overrides(db).overrideFieldParameter(
                field.fieldId, "erythrocytes", 4 + step, "r", MEDTECH_ID, MagicMock())
    assert field.effectiveCounts["erythrocytes"] == 6


def test_theOverrideRequestSchemasAreTheFoundationOnes():
    assert FieldOverrideRequest(particleType="crystals", correctedValue=1000, rationale="r").correctedValue == 1000
    assert RevertOverrideRequest(particleType="crystals", rationale="r").particleType == "crystals"


# ── markVerified (R5 + Q6) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verifyStraightAfterOwnCorrectionSucceeds():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    await corrections(db).saveAnnotations(field.fieldId, MEDTECH_ID, body(removed=["d1"]), MagicMock())
    service = corrections(db)

    response = await service.markVerified(field.fieldId, MEDTECH_ID, MagicMock())

    assert field.verifiedBy == MEDTECH_ID and field.verifiedAt is not None
    assert response.field.verifiedBy == MEDTECH_ID and response.verifiedFieldCount == 1
    assert field.version == 2                              # verifying is not a correction
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELD_VERIFIED"
    assert kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID), "field_id": str(field.fieldId)}
    assert db.commit.await_count == 2          # one for the save, one for the verify


@pytest.mark.asyncio
async def test_verifyStraightAfterAnOverrideAndARevertAlsoSucceeds():
    field = mkField(ai={"erythrocytes": 9})
    db = CorrectionDb(field)
    await overrides(db).overrideFieldParameter(field.fieldId, "erythrocytes", 4, "r", MEDTECH_ID, MagicMock())
    await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert field.verifiedBy == MEDTECH_ID
    db.overrides[0].overriddenAt = datetime(2000, 1, 1, tzinfo=UTC)
    await overrides(db).revertFieldOverride(field.fieldId, "erythrocytes", "r", MEDTECH_ID, MagicMock())
    assert field.verifiedBy is None                        # the revert cleared the mark
    await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert field.verifiedBy == MEDTECH_ID


@pytest.mark.asyncio
async def test_verifyBeforeAnyOpenIsRefused():
    field = mkField()
    db = CorrectionDb(field)
    service = corrections(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELD_NOT_OPENED")
    assert field.verifiedBy is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aPlainOpenIsEnoughWhenNothingWasCorrected():
    field = mkField(lastOpenedBy=MEDTECH_ID, lastOpenedAt=NOW)
    db = CorrectionDb(field)
    await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert field.verifiedBy == MEDTECH_ID


@pytest.mark.asyncio
async def test_anOpenBeforeTheLastCorrectionDoesNotCount():
    field = mkField(lastOpenedBy=MEDTECH_ID, lastOpenedAt=NOW, lastCorrectedAt=NOW + timedelta(minutes=1))
    db = CorrectionDb(field)
    with pytest.raises(HTTPException) as excInfo:
        await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "FIELD_NOT_OPENED"


@pytest.mark.asyncio
async def test_anOpenAtTheSameInstantAsTheCorrectionCounts():
    field = mkField(lastOpenedBy=MEDTECH_ID, lastOpenedAt=NOW, lastCorrectedAt=NOW)
    db = CorrectionDb(field)
    await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert field.verifiedBy == MEDTECH_ID


@pytest.mark.asyncio
async def test_anotherUsersCorrectionBlocksVerifyUntilTheCallerReopens():
    field = mkField(ai={"erythrocytes": 9}, lastOpenedBy=MEDTECH_ID, lastOpenedAt=NOW - timedelta(minutes=2))
    db = CorrectionDb(field)
    # someone else corrects the field: the correction records *them* as the opener
    markOther = FieldCorrectionServiceHelper.correctAs(field, OTHER_ID, NOW)
    assert markOther is None
    service = corrections(db)

    with pytest.raises(HTTPException) as excInfo:
        await service.markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "FIELD_NOT_OPENED"

    # the caller opens it again after the other correction: now it works
    field.lastOpenedBy, field.lastOpenedAt = MEDTECH_ID, NOW + timedelta(minutes=1)
    await service.markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert field.verifiedBy == MEDTECH_ID


@pytest.mark.asyncio
async def test_anotherUsersOpenDoesNotHelpTheCaller():
    field = mkField(lastOpenedBy=OTHER_ID, lastOpenedAt=NOW + timedelta(minutes=5))
    db = CorrectionDb(field)
    with pytest.raises(HTTPException) as excInfo:
        await corrections(db).markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "FIELD_NOT_OPENED"


@pytest.mark.asyncio
async def test_aFieldAlreadyVerifiedIsReturnedUnchanged():
    field = mkField(verifiedBy=MEDTECH_ID, verifiedAt=NOW)
    db = CorrectionDb(field)
    service = corrections(db)
    response = await service.markVerified(field.fieldId, MEDTECH_ID, MagicMock())
    assert response.field.verifiedBy == MEDTECH_ID
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()


class FieldCorrectionServiceHelper:
    @staticmethod
    def correctAs(field: ResultField, who: uuid.UUID, now: datetime) -> None:
        from src.services.field_correction_service import markCorrected
        markCorrected(field, who, now)


# ── discardField: the web path ───────────────────────────────────────────────

def _discardService(db) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=audit(), _notifService=MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION])
async def test_excludingAFieldDiscardsItAndItsImage(status):
    fields = [mkField(n, verifiedBy=MEDTECH_ID, verifiedAt=NOW) for n in (1, 2, 3)]
    db = CorrectionDb(fields[1], fields=fields, result=mkResult(status, fieldCount=3))
    service = _discardService(db)

    response = await service.discardField(fields[1].fieldId, MEDTECH_ID, "  Out of focus  ", MagicMock())

    assert fields[1].status == FieldStatus.DISCARDED and fields[1].discardReason == "Out of focus"
    image = db.images[fields[1].imageId]
    assert image.status == ImageStatus.DISCARDED and image.discardedAt is not None
    assert db.images[fields[0].imageId].status == ImageStatus.ACTIVE
    assert fields[1].version == 2
    assert db.result.fieldCount == 2 and db.result.imageId == fields[0].imageId
    assert response.field.status == FieldStatus.DISCARDED and response.acceptedFieldCount == 2
    assert fields[0].verifiedBy == MEDTECH_ID and fields[2].verifiedBy == MEDTECH_ID   # not a correction
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "FIELD_DISCARDED"
    assert kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID), "field_id": str(fields[1].fieldId)}
    assert "Out of focus" not in str(kwargs)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_discardingTheFirstAcceptedFieldMovesTheImagePointer():
    fields = [mkField(n) for n in (1, 2)]
    db = CorrectionDb(fields[0], fields=fields, result=mkResult(fieldCount=2, imageId=fields[0].imageId))
    await _discardService(db).discardField(fields[0].fieldId, MEDTECH_ID, "Blurry", MagicMock())
    assert db.result.imageId == fields[1].imageId and db.result.fieldCount == 1


@pytest.mark.asyncio
async def test_discardingTheLastAcceptedFieldLeavesTheCountAtZero():
    field = mkField(1)
    db = CorrectionDb(field, result=mkResult(fieldCount=1, imageId=field.imageId))
    await _discardService(db).discardField(field.fieldId, MEDTECH_ID, "Blurry", MagicMock())
    assert db.result.fieldCount == 0 and db.result.imageId == field.imageId


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_aReasonIsRequiredToExcludeAField(reason):
    field = mkField()
    db = CorrectionDb(field)
    service = _discardService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.discardField(field.fieldId, MEDTECH_ID, reason, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (422, "REASON_REQUIRED")
    assert field.status == FieldStatus.ACCEPTED and field.version == 1
    db.commit.assert_not_awaited()
    assert db.images[field.imageId].status == ImageStatus.ACTIVE


@pytest.mark.asyncio
async def test_aFieldAlreadyDiscardedIsReturnedUnchanged():
    field = mkField(status=FieldStatus.DISCARDED)
    db = CorrectionDb(field)
    service = _discardService(db)
    response = await service.discardField(field.fieldId, MEDTECH_ID, "again", MagicMock())
    assert response.field.status == FieldStatus.DISCARDED and field.version == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED, ResultStatus.RELEASED,
    ResultStatus.CRITICAL_ESCALATED, ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.FAILED])
async def test_discardIsRefusedInEveryOtherStatus(status):
    field = mkField()
    db = CorrectionDb(field, result=mkResult(status))
    with pytest.raises(HTTPException) as excInfo:
        await _discardService(db).discardField(field.fieldId, MEDTECH_ID, "reason", MagicMock())
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"


# ── reopenCapture ────────────────────────────────────────────────────────────

def _reopenService(db, notif=None) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=audit(), _notifService=notif or MagicMock(
        notifyMedtechCaptureReopened=AsyncMock()))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION])
async def test_reopenMovesTheResultBackToCapturingAndKeepsEverythingElse(status):
    fields = [mkField(1, verifiedBy=MEDTECH_ID, verifiedAt=NOW), mkField(2)]
    db = CorrectionDb(fields[0], fields=fields, result=mkResult(status, fieldCount=2))
    service = _reopenService(db)

    response = await service.reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())

    assert db.result.status == ResultStatus.CAPTURING and response.status == ResultStatus.CAPTURING
    assert response.resultId == RESULT_ID and response.updatedAt is not None
    assert db.result.fieldCount == 2
    assert fields[0].verifiedBy == MEDTECH_ID and fields[0].version == 1       # corrections and marks kept
    assert db.lockedSpecimen
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reopenClearsTheLegacyFlagAndSaysSoInTheAudit():
    field = mkField()
    db = CorrectionDb(field, result=mkResult(isLegacySingleField=True))
    service = _reopenService(db)

    await service.reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())

    assert db.result.isLegacySingleField is False
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "CAPTURE_REOPENED"
    assert kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID), "field_count": 1, "legacy_cleared": True}


@pytest.mark.asyncio
async def test_reopenLeavesANonLegacyResultNonLegacy():
    db = CorrectionDb(mkField())
    service = _reopenService(db)
    await service.reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())
    assert db.result.isLegacySingleField is False
    assert service.auditLogger.record.call_args.kwargs["detailJson"]["legacy_cleared"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    ResultStatus.CAPTURING, ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED,
    ResultStatus.RELEASED, ResultStatus.CRITICAL_ESCALATED, ResultStatus.IMAGE_RETAKE_REQUESTED,
    ResultStatus.FAILED])
async def test_reopenIsRefusedOutsideTheTwoVerificationStatuses(status):
    notif = MagicMock(notifyMedtechCaptureReopened=AsyncMock())
    db = CorrectionDb(mkField(), result=mkResult(status))
    with pytest.raises(HTTPException) as excInfo:
        await _reopenService(db, notif).reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    assert db.result.status == status
    notif.notifyMedtechCaptureReopened.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopenChecksOwnershipRejectionAndExistence():
    notif = MagicMock(notifyMedtechCaptureReopened=AsyncMock())
    with pytest.raises(HTTPException) as notMine:
        await _reopenService(CorrectionDb(mkField(), specimen=mkSpecimen(medtechId=OTHER_ID)), notif).reopenCapture(
            RESULT_ID, MEDTECH_ID, MagicMock())
    with pytest.raises(HTTPException) as rejected:
        await _reopenService(CorrectionDb(mkField(), specimen=mkSpecimen(status="REJECTED")), notif).reopenCapture(
            RESULT_ID, MEDTECH_ID, MagicMock())
    missing = CorrectionDb(mkField())
    missing.result = None
    with pytest.raises(HTTPException) as gone:
        await _reopenService(missing, notif).reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())

    assert notMine.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    assert rejected.value.errorCode == "SPECIMEN_REJECTED"
    assert (gone.value.status_code, gone.value.errorCode) == (404, "RESULT_NOT_FOUND")
    notif.notifyMedtechCaptureReopened.assert_not_awaited()


@pytest.mark.asyncio
async def test_reopenNotifiesTheMedtechWithTheSampleIdOnly():
    notif = MagicMock(notifyMedtechCaptureReopened=AsyncMock())
    db = CorrectionDb(mkField())

    await _reopenService(db, notif).reopenCapture(RESULT_ID, MEDTECH_ID, MagicMock())

    notif.notifyMedtechCaptureReopened.assert_awaited_once_with(
        medtechId=MEDTECH_ID, resultId=RESULT_ID, sampleUid="SMP-20261027-00007")


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_theReopenNotificationInsertUsesTheNewType():
    db = MagicMock()
    db.begin_nested = lambda: _Nested()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=uuid.uuid4())))

    await NotificationService(db=db).notifyMedtechCaptureReopened(
        medtechId=MEDTECH_ID, resultId=RESULT_ID, sampleUid="SMP-20261027-00007")

    insert = db.execute.await_args_list[0].args[0]
    params = insert.compile().params
    assert params["notification_type"] == "CAPTURE_REOPENED"
    assert params["message"] == "More fields requested for sample SMP-20261027-00007."
    assert params["user_id"] == MEDTECH_ID and params["entity_id"] == RESULT_ID


@pytest.mark.asyncio
async def test_theReopenNotificationCarriesNoPatientData():
    db = MagicMock()
    db.begin_nested = lambda: _Nested()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=uuid.uuid4())))
    await NotificationService(db=db).notifyMedtechCaptureReopened(
        medtechId=MEDTECH_ID, resultId=RESULT_ID, sampleUid="SMP-1")
    message = db.execute.await_args_list[0].args[0].compile().params["message"]
    assert message == "More fields requested for sample SMP-1."


@pytest.mark.asyncio
async def test_aFailedNotificationNeverBreaksTheNotifierItself(caplog):
    db = MagicMock()
    db.begin_nested = lambda: _Nested()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    with caplog.at_level(logging.ERROR):
        await NotificationService(db=db).notifyMedtechCaptureReopened(
            medtechId=MEDTECH_ID, resultId=RESULT_ID, sampleUid="SMP-1")   # does not raise
    assert "Failed to create notification" in caplog.text


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def routeClient(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
    yield TestClient(app)
    for dependency in (getCurrentUser, getFieldCorrectionService, getFieldOverrideService, getResultFieldService):
        app.dependency_overrides.pop(dependency, None)


def _actionResponse():
    from src.schemas.field import FieldActionResponse, FieldSummaryItem
    field = mkField()
    return FieldActionResponse(
        field=FieldSummaryItem(
            fieldId=field.fieldId, resultId=RESULT_ID, fieldNo=1, magnification="HPF", status="ACCEPTED",
            aiFindings={}, effectiveCounts={}, confidenceScores={}, version=1, createdAt=NOW, updatedAt=NOW),
        acceptedFieldCount=1, verifiedFieldCount=0, undecidedFieldCount=0)


def test_theAnnotationsRoutePassesTheBodyToTheService(routeClient):
    service = MagicMock(saveAnnotations=AsyncMock(return_value=_actionResponse()))
    app.dependency_overrides[getFieldCorrectionService] = lambda: service
    fieldId = uuid.uuid4()

    response = routeClient.put(f"/api/v1/fields/{fieldId}/annotations", json={
        "removedDetectionIds": ["d1"], "addedBoxes": [], "expectedVersion": 3})

    assert response.status_code == 200
    args = service.saveAnnotations.await_args.args
    assert args[0] == fieldId and args[1] == MEDTECH_ID
    assert args[2].expectedVersion == 3 and args[2].removedDetectionIds == ["d1"]


def test_theOverrideAndRevertRoutesPassTheirFields(routeClient):
    service = MagicMock(
        overrideFieldParameter=AsyncMock(return_value=_actionResponse()),
        revertFieldOverride=AsyncMock(return_value=_actionResponse()))
    app.dependency_overrides[getFieldOverrideService] = lambda: service
    fieldId = uuid.uuid4()

    assert routeClient.post(f"/api/v1/fields/{fieldId}/override", json={
        "particleType": "crystals", "correctedValue": 7, "rationale": "Recount"}).status_code == 200
    assert routeClient.post(f"/api/v1/fields/{fieldId}/override/revert", json={
        "particleType": "crystals", "rationale": "Boxes were right"}).status_code == 200

    assert service.overrideFieldParameter.await_args.args[:5] == (fieldId, "crystals", 7, "Recount", MEDTECH_ID)
    assert service.revertFieldOverride.await_args.args[:4] == (fieldId, "crystals", "Boxes were right", MEDTECH_ID)


def test_theVerifyRouteCallsMarkVerified(routeClient):
    service = MagicMock(markVerified=AsyncMock(return_value=_actionResponse()))
    app.dependency_overrides[getFieldCorrectionService] = lambda: service
    fieldId = uuid.uuid4()
    assert routeClient.post(f"/api/v1/fields/{fieldId}/verify").status_code == 200
    assert service.markVerified.await_args.args[:2] == (fieldId, MEDTECH_ID)


def test_theDiscardRoutePassesTheReasonOrNone(routeClient):
    service = MagicMock(discardField=AsyncMock(return_value=_actionResponse()))
    app.dependency_overrides[getResultFieldService] = lambda: service
    fieldId = uuid.uuid4()

    assert routeClient.post(f"/api/v1/fields/{fieldId}/discard", json={"reason": "Blurry"}).status_code == 200
    assert routeClient.post(f"/api/v1/fields/{fieldId}/discard").status_code == 200

    reasons = [call.args[2] for call in service.discardField.await_args_list]
    assert reasons == ["Blurry", None]


def test_theReopenRouteCallsReopenCapture(routeClient):
    from src.schemas.field import ResultStatusResponse
    service = MagicMock(reopenCapture=AsyncMock(return_value=ResultStatusResponse(
        resultId=RESULT_ID, status=ResultStatus.CAPTURING, updatedAt=NOW)))
    app.dependency_overrides[getResultFieldService] = lambda: service
    response = routeClient.post(f"/api/v1/results/{RESULT_ID}/reopen-capture")
    assert response.status_code == 200 and response.json()["status"] == "CAPTURING"
    assert service.reopenCapture.await_args.args[:2] == (RESULT_ID, MEDTECH_ID)


@pytest.mark.parametrize(("method", "path"), [
    ("PUT", "/annotations"), ("POST", "/override"), ("POST", "/override/revert"),
    ("POST", "/verify"), ("POST", "/discard"),
])
def test_theCorrectionRoutesAreNotFoundWhileTheFlagIsOff(monkeypatch, method, path):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
    try:
        response = TestClient(app).request(method, f"/api/v1/fields/{uuid.uuid4()}{path}", json={})
    finally:
        app.dependency_overrides.pop(getCurrentUser, None)
    assert response.status_code == 404


def test_theSupervisorCannotCallAnyCorrectionRoute(routeClient):
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(OTHER_ID), "role": "SUPERVISOR"}
    fieldId = uuid.uuid4()
    for method, path in (("PUT", "annotations"), ("POST", "override"), ("POST", "override/revert"),
                         ("POST", "verify"), ("POST", "discard")):
        assert routeClient.request(method, f"/api/v1/fields/{fieldId}/{path}", json={}).status_code == 403
    assert routeClient.post(f"/api/v1/results/{RESULT_ID}/reopen-capture").status_code == 403


@pytest.mark.asyncio
async def test_overrideInForceRendersInTheEnvelope():
    import json
    response = await httpExceptionHandler(None, OverrideInForceError())
    body = json.loads(response.body)
    assert response.status_code == 409 and body["error"]["code"] == "OVERRIDE_IN_FORCE"
