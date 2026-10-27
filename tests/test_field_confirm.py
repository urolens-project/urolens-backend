"""Unit tests — multi-field confirmation and the verification summary (web lane, W3).

`ResultConfirmationService.confirmResult` with the multi-field flag on (and, for
contrast, off), the legacy single-image path, and
`ResultFieldService.getVerificationSummary`, which must agree with confirm.
The session is a small stateful fake; the Smart Diagnosis service is a recorder.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from src.api.fields import getResultFieldService
from src.core.config import settings
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride, OverrideKind
from src.models.result_confirmation import ResultConfirmation
from src.models.result_field import FieldStatus, ResultField
from src.models.specimen import Specimen
from src.schemas.field import VerificationSummaryResponse
from src.services.field_aggregation_service import displayValue
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_field_service import ResultFieldService
from src.services.smart_diagnosis_service import SmartDiagnosisService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000011a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000011a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000011a3")
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000011a4")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000011a5")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)

AI_RBC = (3, 5, 2, 9, 4, 4, 1, 6, 3, 4)
EFFECTIVE_RBC = (3, 5, 2, 6, 4, 4, 1, 6, 3, 4)

MEDTECH = {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
SUPERVISOR = {"user_id": str(SUPERVISOR_ID), "role": "SUPERVISOR"}


# ── builders ─────────────────────────────────────────────────────────────────

def mkField(n, ai=None, effective=None, verified=True, status=FieldStatus.ACCEPTED, **kw) -> ResultField:
    ai = ai if ai is not None else {"erythrocytes": 4}
    return ResultField(
        fieldId=kw.pop("fieldId", uuid.UUID(f"00000000-0000-0000-0000-0000000012{n:02d}")),
        resultId=RESULT_ID, specimenId=SPECIMEN_ID,
        imageId=uuid.UUID(f"00000000-0000-0000-0000-0000000013{n:02d}"),
        fieldNo=n, magnification="HPF", status=status,
        aiFindings=ai, detections=[], imageWidth=800, imageHeight=600, confidenceScores={},
        effectiveCounts=dict(effective if effective is not None else ai),
        smartDiagnosis=None, smartDiagnosisUnavailable=False, modelVersion="mvp-v1.0",
        verifiedBy=MEDTECH_ID if verified else None, version=1, createdAt=NOW, updatedAt=NOW, **kw,
    )


def rbcFields(verified=True) -> list[ResultField]:
    return [mkField(n, {"erythrocytes": a}, {"erythrocytes": e}, verified=verified)
            for n, (a, e) in enumerate(zip(AI_RBC, EFFECTIVE_RBC, strict=True), start=1)]


def mkResult(status=ResultStatus.PENDING_CONFIRM, **kw) -> AnalysisResult:
    result = AnalysisResult(
        resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=status,
        aiFindings=kw.pop("aiFindings", {"erythrocytes": 41}), particleClasses={},
        fieldCount=0, fieldSummary=None, isLegacySingleField=kw.pop("legacy", False),
        imageId=kw.pop("imageId", None), **kw,
    )
    result.manualOverrides = kw.pop("manualOverrides", [])
    return result


def mkSpecimen(medtechId=MEDTECH_ID, status="PROCESSING") -> Specimen:
    return Specimen(specimenId=SPECIMEN_ID, medtechId=medtechId, status=status, sampleUid="SMP-1")


def mkOverride(parameter="crystals", value="7.0", fieldId=None, kind=OverrideKind.OVERRIDE, minutes=0):
    return ManualOverride(
        overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=fieldId, medtechId=MEDTECH_ID,
        parameterName=parameter, originalAiValue="1.0", correctedValue=value, rationale="r",
        kind=kind, overriddenAt=NOW + timedelta(minutes=minutes))


class ConfirmDb:
    """Fake `AsyncSession` for confirm and the summary: routes `execute` by compiled SQL."""

    def __init__(self, result, fields, *, specimen=None, existingConfirmation=None,
                 returnsAfter=0, fieldOverrides=()):
        self.result = result
        self.fields = list(fields)
        self.specimen = specimen if specimen is not None else mkSpecimen()
        self.existingConfirmation = existingConfirmation
        self.returnsAfter = returnsAfter
        self.fieldOverrides = list(fieldOverrides)
        self.added: list = []
        self.statements: list[str] = []
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.refresh = AsyncMock()
        self.lockedSpecimen = False

    async def get(self, model, key, **kwargs):
        if model is AnalysisResult:
            return self.result
        if model is Specimen:
            self.lockedSpecimen = self.lockedSpecimen or bool(kwargs.get("with_for_update"))
            return self.specimen
        return None

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        if "from result_fields" in low and "result_fields.status =" in low:
            out.scalars.return_value.all.return_value = sorted(
                (f for f in self.fields if f.status == FieldStatus.ACCEPTED), key=lambda f: f.fieldNo)
        elif "from result_confirmations" in low:
            out.scalar_one_or_none.return_value = self.existingConfirmation
        elif "count(*)" in low and "result_returns" in low:
            out.scalar_one.return_value = self.returnsAfter
        elif "from manual_overrides" in low and "field_id is null" in low:
            out.scalars.return_value.all.return_value = [
                o for o in self.result.manualOverrides if o.fieldId is None]
        elif "from manual_overrides" in low:
            out.scalars.return_value.all.return_value = self.fieldOverrides
        elif "from analysis_results" in low:
            out.scalar_one_or_none.return_value = self.result
            out.scalar_one.return_value = self.result
        else:  # pragma: no cover - a query the fake does not know about
            raise AssertionError(f"unexpected statement: {low}")
        return out

    @property
    def touchedFields(self) -> bool:
        return any("result_fields" in s.lower() for s in self.statements)


@pytest.fixture(autouse=True)
def multiFieldOn(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    monkeypatch.setattr(settings, "minFieldsPerSpecimen", 10)
    consent = AsyncMock()
    retake = AsyncMock()
    with patch("src.services.result_confirmation_service.requireProcessingConsent", consent), \
         patch("src.services.result_confirmation_service.requireNoPendingRetake", retake), \
         patch("src.services.result_confirmation_service.loadNormalMax", return_value={}), \
         patch("src.services.result_field_service.loadNormalMax", return_value={}):
        yield SimpleNamespace(consent=consent, retake=retake)


def confirmService(db):
    diag = MagicMock(spec=SmartDiagnosisService)
    diag.run = AsyncMock(return_value=None)
    notif = MagicMock(spec=NotificationService)
    notif.notifySupervisorResultReady = AsyncMock()
    service = ResultConfirmationService(
        db=db, auditLogger=MagicMock(record=AsyncMock()), _smartDiagnosisService=diag, _notifService=notif)
    return service, diag, notif


async def confirm(db, notes=None):
    service, diag, notif = confirmService(db)
    request = MagicMock()
    request.client = None
    response = await service.confirmResult(RESULT_ID, MEDTECH_ID, request, interpretationNotes=notes)
    return response, service, diag, notif


def assertNothingWritten(db, service, status):
    assert db.result.status == status
    assert db.added == []
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()


# ── confirm: the worked example, end to end ──────────────────────────────────

@pytest.mark.asyncio
async def test_theProposalExampleThroughConfirm():
    db = ConfirmDb(mkResult(), rbcFields())

    response, service, diag, notif = await confirm(db, notes="No abnormalities.")

    result = db.result
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert result.fieldCount == 10
    assert result.fieldSummary["perClass"]["erythrocytes"] == {
        "mean": 3.8, "min": 1, "max": 6, "total": 38, "fieldsSeen": 10, "aiMean": 4.1}
    assert result.fieldSummary["fieldCount"] == 10 and result.fieldSummary["focalFindings"] == []
    assert result.aiFindings == {"erythrocytes": 4.1}
    assert result.particleClasses == {"erythrocytes": 4}
    assert result.imageId == db.fields[0].imageId
    assert result.confirmedBy == MEDTECH_ID and result.interpretation == "No abnormalities."
    diag.run.assert_awaited_once_with(
        resultId=RESULT_ID, db=db, counts={"erythrocytes": 3.8}, allowDecimal=True)
    notif.notifySupervisorResultReady.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert db.lockedSpecimen


@pytest.mark.asyncio
async def test_theAuditRowCarriesTheFieldCount():
    db = ConfirmDb(mkResult(), rbcFields())
    _response, service, *_ = await confirm(db)
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "RESULT_CONFIRMED"
    assert kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID), "field_count": 10}


@pytest.mark.asyncio
async def test_aRareParticleIsStoredAsOneNeverZero():
    counts = [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    fields = [mkField(n, {"urinary_casts": c}) for n, c in enumerate(counts, start=1)]
    db = ConfirmDb(mkResult(), fields)

    _response, _service, diag, _notif = await confirm(db)

    assert db.result.particleClasses == {"urinary_casts": 1}
    assert db.result.fieldSummary["perClass"]["urinary_casts"]["mean"] == 0.3
    assert diag.run.await_args.kwargs["counts"] == {"urinary_casts": 0.3}


@pytest.mark.asyncio
@pytest.mark.parametrize(("total", "stored"), [(4, 1), (5, 1), (25, 3), (0, 0)])
async def test_halfUpAtTheStoredBoundaries(total, stored):
    # mean = total / 10: 0.4 -> 1, 0.5 -> 1, 2.5 -> 3, 0 -> 0
    counts = [total] + [0] * 9
    fields = [mkField(n, {"yeast": c}) for n, c in enumerate(counts, start=1)]
    db = ConfirmDb(mkResult(), fields)
    await confirm(db)
    assert db.result.particleClasses == {"yeast": stored}


@pytest.mark.asyncio
async def test_aFocalFindingIsStoredWhenTheMeanIsNormal(monkeypatch):
    counts = [0] * 9 + [7]
    fields = [mkField(n, {"crystals": c}) for n, c in enumerate(counts, start=1)]
    db = ConfirmDb(mkResult(), fields)

    with patch("src.services.result_confirmation_service.loadNormalMax", return_value={"crystals": 5.0}):
        await confirm(db)

    assert db.result.fieldSummary["focalFindings"] == [{"particleType": "crystals", "fieldNos": [10]}]
    assert db.result.fieldSummary["perClass"]["crystals"]["mean"] == 0.7


@pytest.mark.asyncio
async def test_noFocalFindingsAndNoErrorWithoutARulesConfig():
    counts = [0] * 9 + [7]
    db = ConfirmDb(mkResult(), [mkField(n, {"crystals": c}) for n, c in enumerate(counts, start=1)])
    await confirm(db)                        # the fixture's loadNormalMax returns {}
    assert db.result.fieldSummary["focalFindings"] == []


@pytest.mark.asyncio
async def test_theAiOutputOnTheFieldsIsNotTouched():
    fields = rbcFields()
    before = [(dict(f.aiFindings), list(f.detections)) for f in fields]
    await confirm(ConfirmDb(mkResult(), fields))
    assert [(dict(f.aiFindings), list(f.detections)) for f in fields] == before


@pytest.mark.asyncio
async def test_theMinimumComesFromSettings(monkeypatch):
    monkeypatch.setattr(settings, "minFieldsPerSpecimen", 3)
    db = ConfirmDb(mkResult(), rbcFields()[:3])
    response, *_ = await confirm(db)
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert db.result.fieldCount == 3


# ── confirm: guards ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_belowTheMinimumIsRefusedAndNothingIsWritten():
    db = ConfirmDb(mkResult(), rbcFields()[:9])
    service, diag, _ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELDS_BELOW_MINIMUM")
    assert "9 of 10" in excInfo.value.detail
    assertNothingWritten(db, service, ResultStatus.PENDING_CONFIRM)
    diag.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_aDiscardedOrAnalysedFieldDoesNotCountTowardTheMinimum():
    fields = rbcFields()[:9] + [mkField(10, status=FieldStatus.DISCARDED), mkField(11, status=FieldStatus.ANALYZED)]
    db = ConfirmDb(mkResult(), fields)
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "FIELDS_BELOW_MINIMUM"


@pytest.mark.asyncio
async def test_anUnverifiedAcceptedFieldIsRefused():
    fields = rbcFields()
    fields[4].verifiedBy = None
    db = ConfirmDb(mkResult(), fields)
    service, diag, _ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "FIELDS_NOT_VERIFIED")
    assertNothingWritten(db, service, ResultStatus.PENDING_CONFIRM)
    diag.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_aDiscardedFieldNeedsNoVerification():
    fields = rbcFields() + [mkField(11, status=FieldStatus.DISCARDED, verified=False)]
    response, *_ = await confirm(ConfirmDb(mkResult(), fields))
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL


@pytest.mark.asyncio
async def test_anotherMedtechIsForbiddenBeforeAnyStateCheck():
    db = ConfirmDb(mkResult(ResultStatus.APPROVED), rbcFields(), specimen=mkSpecimen(medtechId=OTHER_ID))
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (403, "SPECIMEN_NOT_ASSIGNED")


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [
    (ResultStatus.CAPTURING, "RESULT_NOT_CONFIRMABLE"),
    (ResultStatus.IMAGE_RETAKE_REQUESTED, "RESULT_NOT_CONFIRMABLE"),
    (ResultStatus.FAILED, "RESULT_NOT_CONFIRMABLE"),
    (ResultStatus.PENDING_SUPERVISOR_APPROVAL, "RESULT_ALREADY_CONFIRMED"),
    (ResultStatus.APPROVED, "RESULT_ALREADY_CONFIRMED"),
    (ResultStatus.RELEASED, "RESULT_ALREADY_CONFIRMED"),
])
async def test_onlyTheTwoVerificationStatusesCanBeConfirmedWhenNotLegacy(status, code):
    db = ConfirmDb(mkResult(status), rbcFields())
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == code
    assert db.result.status == status


@pytest.mark.asyncio
async def test_aRejectedSpecimenCannotBeConfirmed():
    db = ConfirmDb(mkResult(), rbcFields(), specimen=mkSpecimen(status="REJECTED"))
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"
    assertNothingWritten(db, service, ResultStatus.PENDING_CONFIRM)


@pytest.mark.asyncio
async def test_consentIsStillChecked(multiFieldOn):
    multiFieldOn.consent.side_effect = HTTPException(status_code=409, detail="no consent")
    db = ConfirmDb(mkResult(), rbcFields())
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException):
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    multiFieldOn.consent.assert_awaited_once()
    assertNothingWritten(db, service, ResultStatus.PENDING_CONFIRM)


@pytest.mark.asyncio
async def test_thePendingRetakeGuardIsSkippedWithTheFlagOn(multiFieldOn):
    await confirm(ConfirmDb(mkResult(imageId=uuid.uuid4()), rbcFields()))
    multiFieldOn.retake.assert_not_awaited()


@pytest.mark.asyncio
async def test_theSpecimenIsLockedBeforeTheFieldsAreRead():
    db = ConfirmDb(mkResult(), rbcFields())
    await confirm(db)
    assert db.lockedSpecimen


# ── confirm: resubmission is derived from the returns ────────────────────────

@pytest.mark.asyncio
async def test_aFirstConfirmationIsNotAResubmission():
    response, service, *_ = await confirm(ConfirmDb(mkResult(), rbcFields()))
    assert response.resubmitted is False
    assert service.auditLogger.record.call_args.kwargs["eventType"] == "RESULT_CONFIRMED"


@pytest.mark.asyncio
async def test_aReturnAfterTheLastConfirmationMakesTheNextOneAResubmissionEvenAfterAReopen():
    earlier = ResultConfirmation(confirmationId=uuid.uuid4(), resultId=RESULT_ID, medtechId=MEDTECH_ID,
                                 confirmedAt=NOW - timedelta(days=1))
    # After the return the MedTech reopened capture and re-submitted: the status is
    # PENDING_CONFIRM again, not RETURNED_FOR_CORRECTION.
    db = ConfirmDb(mkResult(ResultStatus.PENDING_CONFIRM), rbcFields(),
                   existingConfirmation=earlier, returnsAfter=1)

    response, service, *_ = await confirm(db)

    assert response.resubmitted is True
    assert service.auditLogger.record.call_args.kwargs["eventType"] == "RESULT_RESUBMITTED"
    assert earlier.confirmedAt > NOW - timedelta(days=1)          # the row is replaced in place
    assert db.added == []                                          # no second confirmation row


@pytest.mark.asyncio
async def test_aConfirmationWithNoReturnAfterItIsNotAResubmissionWhateverTheStatus():
    earlier = ResultConfirmation(confirmationId=uuid.uuid4(), resultId=RESULT_ID, medtechId=MEDTECH_ID,
                                 confirmedAt=NOW - timedelta(days=1))
    db = ConfirmDb(mkResult(ResultStatus.RETURNED_FOR_CORRECTION), rbcFields(),
                   existingConfirmation=earlier, returnsAfter=0)
    response, service, *_ = await confirm(db)
    assert response.resubmitted is False
    assert service.auditLogger.record.call_args.kwargs["eventType"] == "RESULT_CONFIRMED"


@pytest.mark.asyncio
async def test_aReturnedResultWithAReturnAfterItsConfirmationIsAResubmission():
    earlier = ResultConfirmation(confirmationId=uuid.uuid4(), resultId=RESULT_ID, medtechId=MEDTECH_ID,
                                 confirmedAt=NOW - timedelta(days=1))
    db = ConfirmDb(mkResult(ResultStatus.RETURNED_FOR_CORRECTION), rbcFields(),
                   existingConfirmation=earlier, returnsAfter=1)
    response, service, *_ = await confirm(db)
    assert response.resubmitted is True
    assert service.auditLogger.record.call_args.kwargs["detailJson"]["field_count"] == 10


@pytest.mark.asyncio
async def test_theReturnLookupIsSkippedWhenThereIsNoEarlierConfirmation():
    db = ConfirmDb(mkResult(), rbcFields(), returnsAfter=5)       # would say "resubmission" if asked
    response, *_ = await confirm(db)
    assert response.resubmitted is False
    assert not any("result_returns" in s.lower() for s in db.statements)


# ── confirm: the legacy single-image path ────────────────────────────────────

LEGACY_STATUSES = [
    ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION,
    ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.FAILED,
]


def legacyDb(status=ResultStatus.PENDING_CONFIRM, ai=None, overrides=(), fields=None, **kw):
    ai = {"erythrocytes": 12, "crystals": 1} if ai is None else ai
    field = mkField(1, ai, verified=False)
    return ConfirmDb(
        mkResult(status, legacy=True, aiFindings=ai, manualOverrides=list(overrides)),
        [field] if fields is None else fields, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LEGACY_STATUSES)
async def test_aLegacyResultWithOneFieldConfirmsFromEachOfTheFourStatuses(status):
    db = legacyDb(status)

    response, service, diag, _notif = await confirm(db)

    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert db.result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    diag.run.assert_awaited_once_with(resultId=RESULT_ID, db=db)         # as today: no decimals
    kwargs = service.auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "RESULT_CONFIRMED"
    assert kwargs["detailJson"] == {
        "specimen_id": str(SPECIMEN_ID), "field_count": 1, "legacy_single_field": True}
    assert "LEGACY" not in kwargs["eventType"]


@pytest.mark.asyncio
async def test_aLegacyConfirmSettlesCountsAsItAlwaysDid():
    db = legacyDb(overrides=[mkOverride("crystals", "7.0"), mkOverride("crystals", "9.0", minutes=1)])

    await confirm(db)

    assert db.result.particleClasses == {"erythrocytes": 12, "crystals": 9.0}
    assert db.result.fieldCount == 1
    assert db.result.fieldSummary is None
    assert db.result.aiFindings == {"erythrocytes": 12, "crystals": 1}      # untouched


@pytest.mark.asyncio
async def test_aLegacyConfirmSkipsTheMinimumAndTheVerifyRules():
    db = legacyDb()                                    # one field, not verified
    response, *_ = await confirm(db)
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL


@pytest.mark.asyncio
async def test_aLegacyResubmissionIsRecordedAsOneAndStillMarkedLegacy():
    earlier = ResultConfirmation(confirmationId=uuid.uuid4(), resultId=RESULT_ID, medtechId=MEDTECH_ID,
                                 confirmedAt=NOW - timedelta(days=1))
    db = legacyDb(ResultStatus.RETURNED_FOR_CORRECTION, existingConfirmation=earlier, returnsAfter=1)
    response, service, *_ = await confirm(db)
    kwargs = service.auditLogger.record.call_args.kwargs
    assert response.resubmitted is True
    assert kwargs["eventType"] == "RESULT_RESUBMITTED"
    assert kwargs["detailJson"]["legacy_single_field"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LEGACY_STATUSES)
async def test_aLegacyResultWithNoAcceptedFieldIsRefused(status):
    db = legacyDb(status, fields=[mkField(1, status=FieldStatus.DISCARDED)])
    service, diag, _ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "LEGACY_NO_FIELD")
    assertNothingWritten(db, service, status)
    diag.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_aLegacyResultWithNoFieldsAtAllIsRefused():
    db = legacyDb(fields=[])
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "LEGACY_NO_FIELD"


@pytest.mark.asyncio
async def test_emptyFindingsAndNoOverrideAreRefused():
    db = legacyDb(ResultStatus.FAILED, ai={})
    service, diag, _ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (409, "LEGACY_NO_FINDINGS")
    assertNothingWritten(db, service, ResultStatus.FAILED)
    diag.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_emptyFindingsWithAResultLevelOverrideConfirm():
    db = legacyDb(ai={}, overrides=[mkOverride("crystals", "3.0")])
    response, *_ = await confirm(db)
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert db.result.particleClasses == {"crystals": 3.0}


@pytest.mark.asyncio
async def test_aFieldLevelOverrideDoesNotCountAsAResultLevelOne():
    db = legacyDb(ai={}, overrides=[mkOverride("crystals", "3.0", fieldId=uuid.uuid4())])
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "LEGACY_NO_FINDINGS"


@pytest.mark.asyncio
async def test_anAllZeroFindingsDictStillConfirms():
    db = legacyDb(ai={"erythrocytes": 0, "crystals": 0})
    response, *_ = await confirm(db)
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert db.result.particleClasses == {"erythrocytes": 0, "crystals": 0}


@pytest.mark.asyncio
async def test_noFieldsIsCheckedBeforeNoFindings():
    db = legacyDb(ai={}, fields=[])
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "LEGACY_NO_FIELD"


@pytest.mark.asyncio
async def test_aLegacyFlagWithSeveralAcceptedFieldsFollowsTheMultiFieldRules():
    fields = [mkField(n, verified=False) for n in (1, 2, 3)]
    db = legacyDb(fields=fields)
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "FIELDS_BELOW_MINIMUM"


@pytest.mark.asyncio
async def test_aNonLegacyResultInARetakeStatusIsStillNotConfirmable():
    db = ConfirmDb(mkResult(ResultStatus.IMAGE_RETAKE_REQUESTED), [mkField(1)])
    service, *_ = confirmService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
    assert excInfo.value.errorCode == "RESULT_NOT_CONFIRMABLE"


# ── confirm with the flag off ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_withTheFlagOffConfirmDoesNotLookAtFieldsAtAll(monkeypatch, multiFieldOn):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    db = ConfirmDb(mkResult(ResultStatus.RETURNED_FOR_CORRECTION, legacy=True,
                            aiFindings={"RBC": 12}), [])
    db.result.manualOverrides = []

    response, service, diag, _ = await confirm(db)

    assert not db.touchedFields
    assert response.resubmitted is True                      # still derived from the status
    assert service.auditLogger.record.call_args.kwargs["detailJson"] == {"specimen_id": str(SPECIMEN_ID)}
    assert service.auditLogger.record.call_args.kwargs["eventType"] == "RESULT_RESUBMITTED"
    diag.run.assert_awaited_once_with(resultId=RESULT_ID, db=db)
    multiFieldOn.retake.assert_awaited_once()                # the retake guard still runs
    assert db.result.particleClasses == {"RBC": 12}
    assert db.result.fieldSummary is None and db.result.fieldCount == 0


# ── the verification summary ─────────────────────────────────────────────────

def summaryService(db) -> ResultFieldService:
    return ResultFieldService(db=db, auditLogger=MagicMock(record=AsyncMock()), _notifService=MagicMock())


def withEngine(output=None, error=None):
    engineOutput = output
    if engineOutput is None:
        condition = SimpleNamespace(level=SimpleNamespace(value="LOW"), weighted_score=0.0, evidence=[])
        engineOutput = SimpleNamespace(
            gout=condition, glomerulonephritis=condition, nephrolithiasis=condition,
            no_significant_indicators=True, engine_version="t")
    engine = MagicMock(return_value=engineOutput, side_effect=error)
    return patch("urolens_ai.generate_smart_diagnosis", engine), engine


@pytest.mark.asyncio
async def test_theSummaryGivesTheProposalNumbers():
    db = ConfirmDb(mkResult(), rbcFields())
    patcher, engine = withEngine()
    with patcher:
        summary = await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)

    rbc = summary.perClass["erythrocytes"]
    assert isinstance(summary, VerificationSummaryResponse)
    assert (rbc.mean, rbc.min, rbc.max, rbc.total, rbc.fieldsSeen, rbc.aiMean) == (3.8, 1, 6, 38, 10, 4.1)
    assert rbc.displayValue == "4"
    assert (summary.fieldCount, summary.minFields) == (10, 10)
    assert summary.canConfirm is True and summary.blockingReason is None
    engine.assert_called_once_with({"erythrocytes": 3.8})
    assert summary.smartDiagnosisUnavailable is False
    assert set(summary.smartDiagnosisPreview) >= {"gout", "no_significant_indicators"}


@pytest.mark.asyncio
async def test_theSummaryNumbersEqualWhatConfirmStores():
    fields = rbcFields()
    summaryDb = ConfirmDb(mkResult(), fields)
    patcher, _ = withEngine()
    with patcher:
        summary = await summaryService(summaryDb).getVerificationSummary(RESULT_ID, MEDTECH)
    confirmDb = ConfirmDb(mkResult(), fields)
    await confirm(confirmDb)

    stored = confirmDb.result.fieldSummary
    for particleType, shown in summary.perClass.items():
        assert stored["perClass"][particleType] == {
            "mean": shown.mean, "min": shown.min, "max": shown.max, "total": shown.total,
            "fieldsSeen": shown.fieldsSeen, "aiMean": shown.aiMean}
        assert confirmDb.result.particleClasses[particleType] == int(shown.displayValue)
    assert stored["fieldCount"] == summary.fieldCount
    assert stored["focalFindings"] == [
        {"particleType": f.particleType, "fieldNos": f.fieldNos} for f in summary.focalFindings]


@pytest.mark.asyncio
async def test_theSummaryShowsALessThanOneAndTheFocalFinding():
    counts = [0] * 9 + [7]
    fields = [mkField(n, {"crystals": c, "yeast": 1 if n <= 3 else 0}) for n, c in enumerate(counts, start=1)]
    db = ConfirmDb(mkResult(), fields)
    patcher, _ = withEngine()
    with patcher, patch("src.services.result_field_service.loadNormalMax", return_value={"crystals": 5.0}):
        summary = await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)

    assert summary.perClass["yeast"].displayValue == "<1" and summary.perClass["yeast"].mean == 0.3
    assert summary.perClass["crystals"].fieldsAboveNormal == [10]
    assert [(f.particleType, f.fieldNos) for f in summary.focalFindings] == [("crystals", [10])]


@pytest.mark.asyncio
async def test_theSummaryWritesNothing():
    db = ConfirmDb(mkResult(), rbcFields())
    patcher, _ = withEngine()
    with patcher:
        await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)
    assert db.added == []
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()
    assert db.result.fieldSummary is None and db.result.particleClasses == {}
    assert db.result.status == ResultStatus.PENDING_CONFIRM


@pytest.mark.asyncio
async def test_aPreviewFailureIsUnavailableNotAnError():
    db = ConfirmDb(mkResult(), rbcFields())
    patcher, _ = withEngine(error=RuntimeError("decimal counts are not supported"))
    with patcher:
        summary = await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)
    assert summary.smartDiagnosisPreview is None and summary.smartDiagnosisUnavailable is True
    assert summary.canConfirm is True                  # the diagnosis never blocks confirmation


@pytest.mark.asyncio
async def test_noAcceptedFieldsMeansNoPreview():
    db = ConfirmDb(mkResult(), [])
    patcher, engine = withEngine()
    with patcher:
        summary = await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)
    engine.assert_not_called()
    assert (summary.smartDiagnosisPreview, summary.smartDiagnosisUnavailable) == (None, False)
    assert summary.perClass == {} and summary.fieldCount == 0


# every blocking reason is reachable, and they come in the documented order

async def _summary(db):
    patcher, _ = withEngine()
    with patcher:
        return await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)


@pytest.mark.asyncio
async def test_resultNotConfirmable():
    summary = await _summary(ConfirmDb(mkResult(ResultStatus.CAPTURING), rbcFields()))
    assert (summary.canConfirm, summary.blockingReason) == (False, "RESULT_NOT_CONFIRMABLE")


@pytest.mark.asyncio
async def test_resultNotConfirmableForARejectedSpecimen():
    summary = await _summary(ConfirmDb(mkResult(), rbcFields(), specimen=mkSpecimen(status="REJECTED")))
    assert summary.blockingReason == "RESULT_NOT_CONFIRMABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.FAILED])
async def test_aRetakeStatusIsNotConfirmableUnlessTheResultIsLegacy(status):
    plain = await _summary(ConfirmDb(mkResult(status), rbcFields()))
    legacy = await _summary(legacyDb(status))
    assert plain.blockingReason == "RESULT_NOT_CONFIRMABLE"
    assert (legacy.canConfirm, legacy.blockingReason) == (True, None)


@pytest.mark.asyncio
async def test_legacyNoField():
    summary = await _summary(legacyDb(fields=[]))
    assert (summary.canConfirm, summary.blockingReason) == (False, "LEGACY_NO_FIELD")


@pytest.mark.asyncio
async def test_legacyNoFindings():
    summary = await _summary(legacyDb(ai={}))
    assert (summary.canConfirm, summary.blockingReason) == (False, "LEGACY_NO_FINDINGS")


@pytest.mark.asyncio
async def test_legacyWithAnOverrideInsteadOfFindingsCanConfirm():
    summary = await _summary(legacyDb(ai={}, overrides=[mkOverride("crystals", "3.0")]))
    assert (summary.canConfirm, summary.blockingReason) == (True, None)
    assert summary.perClass["crystals"].mean == 3.0


@pytest.mark.asyncio
async def test_fieldsBelowMinimum():
    summary = await _summary(ConfirmDb(mkResult(), rbcFields()[:9]))
    assert (summary.canConfirm, summary.blockingReason) == (False, "FIELDS_BELOW_MINIMUM")


@pytest.mark.asyncio
async def test_fieldsNotVerified():
    fields = rbcFields()
    fields[0].verifiedBy = None
    summary = await _summary(ConfirmDb(mkResult(), fields))
    assert (summary.canConfirm, summary.blockingReason) == (False, "FIELDS_NOT_VERIFIED")


@pytest.mark.asyncio
async def test_theReasonsComeInTheDocumentedOrder():
    unverifiedShort = rbcFields(verified=False)[:5]
    # status beats everything
    assert (await _summary(ConfirmDb(mkResult(ResultStatus.APPROVED), unverifiedShort))).blockingReason == \
        "RESULT_NOT_CONFIRMABLE"
    # the minimum beats "not verified"
    assert (await _summary(ConfirmDb(mkResult(), unverifiedShort))).blockingReason == "FIELDS_BELOW_MINIMUM"
    # legacy: no field beats no findings
    assert (await _summary(legacyDb(ai={}, fields=[]))).blockingReason == "LEGACY_NO_FIELD"
    # status beats the legacy reasons too
    assert (await _summary(legacyDb(ResultStatus.APPROVED, fields=[]))).blockingReason == \
        "RESULT_NOT_CONFIRMABLE"


@pytest.mark.asyncio
async def test_theSummaryAgreesWithConfirmOnEveryCase():
    cases = [
        ConfirmDb(mkResult(), rbcFields()),
        ConfirmDb(mkResult(), rbcFields()[:9]),
        ConfirmDb(mkResult(), [*rbcFields()[:9], mkField(10, verified=False)]),
        legacyDb(), legacyDb(fields=[]), legacyDb(ai={}),
        legacyDb(ResultStatus.FAILED), ConfirmDb(mkResult(ResultStatus.FAILED), rbcFields()),
    ]
    for db in cases:
        summary = await _summary(db)
        service, *_ = confirmService(db)
        try:
            await service.confirmResult(RESULT_ID, MEDTECH_ID, MagicMock())
            confirmed = True
        except HTTPException as exc:
            confirmed, code = False, exc.errorCode
        assert summary.canConfirm is confirmed
        if not confirmed:
            assert {"FIELDS_BELOW_MINIMUM": "FIELDS_BELOW_MINIMUM", "FIELDS_NOT_VERIFIED": "FIELDS_NOT_VERIFIED",
                    "LEGACY_NO_FIELD": "LEGACY_NO_FIELD", "LEGACY_NO_FINDINGS": "LEGACY_NO_FINDINGS",
                    "RESULT_NOT_CONFIRMABLE": "RESULT_NOT_CONFIRMABLE"}[summary.blockingReason] == code


@pytest.mark.asyncio
async def test_correctionsListBoxEditsAndOverridesAgainstTheAiCount():
    boxed = mkField(1, {"erythrocytes": 9, "crystals": 1}, {"erythrocytes": 6, "crystals": 1})
    overridden = mkField(2, {"erythrocytes": 4}, {"erythrocytes": 7})
    untouched = mkField(3, {"erythrocytes": 4})
    override = ManualOverride(
        overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=overridden.fieldId, medtechId=MEDTECH_ID,
        parameterName="erythrocytes", originalAiValue="4.0", correctedValue="7.0", rationale="r",
        kind=OverrideKind.OVERRIDE, overriddenAt=NOW)
    db = ConfirmDb(mkResult(), [boxed, overridden, untouched], fieldOverrides=[override])

    summary = await _summary(db)

    assert [(c.fieldNo, c.particleType, c.aiValue, c.effectiveValue, c.kind) for c in summary.corrections] == [
        (1, "erythrocytes", 9, 6, "BOXES"), (2, "erythrocytes", 4, 7, "OVERRIDE")]


@pytest.mark.asyncio
async def test_aRevertedOverrideIsListedAsBoxes():
    field = mkField(1, {"erythrocytes": 9}, {"erythrocytes": 6})
    rows = [
        ManualOverride(overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=field.fieldId, medtechId=MEDTECH_ID,
                       parameterName="erythrocytes", originalAiValue="9.0", correctedValue="2.0", rationale="r",
                       kind=OverrideKind.OVERRIDE, overriddenAt=NOW),
        ManualOverride(overrideId=uuid.uuid4(), resultId=RESULT_ID, fieldId=field.fieldId, medtechId=MEDTECH_ID,
                       parameterName="erythrocytes", originalAiValue="9.0", correctedValue="6.0", rationale="r",
                       kind=OverrideKind.REVERT, overriddenAt=NOW + timedelta(minutes=1)),
    ]
    summary = await _summary(ConfirmDb(mkResult(), [field], fieldOverrides=rows))
    assert [c.kind for c in summary.corrections] == ["BOXES"]


@pytest.mark.asyncio
async def test_aMedtechSeesOnlyAnAssignedSpecimenASupervisorSeesAny():
    db = ConfirmDb(mkResult(), rbcFields(), specimen=mkSpecimen(medtechId=OTHER_ID))
    patcher, _ = withEngine()
    with patcher:
        with pytest.raises(HTTPException) as excInfo:
            await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)
        assert (excInfo.value.status_code, excInfo.value.errorCode) == (403, "SPECIMEN_NOT_ASSIGNED")
        for status in (ResultStatus.PENDING_CONFIRM, ResultStatus.CAPTURING, ResultStatus.APPROVED):
            db.result.status = status
            assert (await summaryService(db).getVerificationSummary(RESULT_ID, SUPERVISOR)).fieldCount == 10


@pytest.mark.asyncio
async def test_aSummaryForAMissingResultIs404():
    db = ConfirmDb(mkResult(), [])
    db.result = None
    with pytest.raises(HTTPException) as excInfo:
        await summaryService(db).getVerificationSummary(RESULT_ID, MEDTECH)
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (404, "RESULT_NOT_FOUND")


# ── the route ────────────────────────────────────────────────────────────────

@pytest.fixture
def routeClient():
    app.dependency_overrides[getCurrentUser] = lambda: MEDTECH
    yield TestClient(app)
    app.dependency_overrides.pop(getCurrentUser, None)
    app.dependency_overrides.pop(getResultFieldService, None)


def test_theSummaryRouteCallsTheService(routeClient):
    summary = VerificationSummaryResponse(
        resultId=RESULT_ID, fieldCount=0, minFields=10, perClass={}, canConfirm=False,
        blockingReason="FIELDS_BELOW_MINIMUM")
    service = MagicMock(getVerificationSummary=AsyncMock(return_value=summary))
    app.dependency_overrides[getResultFieldService] = lambda: service

    response = routeClient.get(f"/api/v1/results/{RESULT_ID}/summary")

    assert response.status_code == 200
    assert response.json()["blockingReason"] == "FIELDS_BELOW_MINIMUM"
    assert service.getVerificationSummary.await_args.args == (RESULT_ID, MEDTECH)


def test_theSummaryRouteIsNotFoundWhileTheFlagIsOff(routeClient, monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    assert routeClient.get(f"/api/v1/results/{RESULT_ID}/summary").status_code == 404


def test_theSummaryRouteAdmitsSupervisorsButNotOtherRoles(routeClient):
    summary = VerificationSummaryResponse(
        resultId=RESULT_ID, fieldCount=0, minFields=10, perClass={}, canConfirm=False)
    app.dependency_overrides[getResultFieldService] = lambda: MagicMock(
        getVerificationSummary=AsyncMock(return_value=summary))
    app.dependency_overrides[getCurrentUser] = lambda: SUPERVISOR
    assert routeClient.get(f"/api/v1/results/{RESULT_ID}/summary").status_code == 200
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(OTHER_ID), "role": "PATIENT"}
    assert routeClient.get(f"/api/v1/results/{RESULT_ID}/summary").status_code == 403


def test_theNewBlockingReasonsAreInTheSchema():
    reasons = VerificationSummaryResponse.model_json_schema()["properties"]["blockingReason"]
    allowed = {v for part in reasons["anyOf"] for v in part.get("enum", [])}
    assert allowed == {
        "RESULT_NOT_CONFIRMABLE", "LEGACY_NO_FIELD", "LEGACY_NO_FINDINGS",
        "FIELDS_BELOW_MINIMUM", "FIELDS_NOT_VERIFIED"}


def test_displayValueMatchesTheStoredWholeNumber():
    assert displayValue(3.8) == "4" and displayValue(0.3) == "<1" and displayValue(0) == "0"
