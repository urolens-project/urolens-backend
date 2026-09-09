"""Unit tests — ResultReviewService (supervisor review/approval, plan row 7)

Tier-1 workflow (standards skill's named example: confirm -> override ->
approve -> release). Baseline coverage per the original task's Task 5, not
exhaustive: one happy-path test per state-transition action
(approve/return/escalate), plus the guard shared by all three
(_require_pending rejects a result that isn't PENDING_SUPERVISOR_APPROVAL)
and the one input-validation branch escalate_result has that the others
don't.

save_annotation/get_full_result gained targeted coverage in a follow-up
fix for a real regression: spatial_annotations was accepted by
save_annotation but never actually persisted (the SQLAlchemy port never
assigned it onto the ResultReview object it saved), so every supervisor
annotation's spatial data was silently dropped. See
test_annotate_result_persists_and_round_trips_spatial_annotations below —
this is the test that would have caught it, and does: it fails against
the pre-fix code (asserting on the object passed to db.add, not just the
call succeeding) and passes against the fix. The rest of
ResultReviewService's read-side methods (get_pending, get_approved_today,
get_escalated, get_supervisor_stats) remain untested — still a gap, not
attempted here either, out of scope for the regression fix.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import ConflictException, NotFoundException, UnprocessableException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.escalation import Escalation
from src.models.manual_override import ManualOverride
from src.models.patient import Patient
from src.models.result_approval import ResultApproval
from src.models.result_return import ResultReturn
from src.models.result_review import ResultReview
from src.models.specimen import Specimen
from src.models.user import User
from src.services.result_review_service import ResultReviewService, getSmartDiagnosis

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000031")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000032")


def _makeResult(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    return result


def _makeSpecimen() -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.completedAt = None
    return specimen


def _makeDbMock(getSideEffect: list) -> AsyncMock:
    """db.get(Model, id) returns the next item in get_side_effect, in call order."""
    db = AsyncMock()
    db.get = AsyncMock(side_effect=getSideEffect)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_approveResultTransitionsStatusAndCompletesSpecimen():
    """Happy path: approving a pending result records the approval, moves the
    result to APPROVED, and marks its specimen COMPLETED so it leaves the
    medtech's active queue.
    """
    result = _makeResult()
    specimen = _makeSpecimen()
    db = _makeDbMock(getSideEffect=[result, specimen])

    _service = ResultReviewService(db=db)
    response = await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes="Looks good")

    assert result.status == ResultStatus.APPROVED
    assert specimen.status == "COMPLETED"
    assert specimen.completedAt is not None
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.APPROVED.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultApproval)
    assert added.resultId == RESULT_ID
    assert added.approvedBy == SUPERVISOR_ID
    assert added.notes == "Looks good"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_returnResultTransitionsStatusAndRecordsReason():
    """Happy path: returning a pending result records the return reason and
    moves the result to RETURNED_FOR_CORRECTION.
    """
    result = _makeResult()
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    response = await _service.returnResult(RESULT_ID, SUPERVISOR_ID, reason="Blurry image")

    assert result.status == ResultStatus.RETURNED_FOR_CORRECTION
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.RETURNED_FOR_CORRECTION.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultReturn)
    assert added.resultId == RESULT_ID
    assert added.returnedBy == SUPERVISOR_ID
    assert added.reason == "Blurry image"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalateResultTransitionsStatusAndRecordsPath():
    """Happy path: escalating a pending result records the escalation path
    and moves the result to CRITICAL_ESCALATED.
    """
    result = _makeResult()
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    response = await _service.escalateResult(
        RESULT_ID, SUPERVISOR_ID, escalationPath="MARK_CRITICAL", escalationNote="Urgent"
    )

    assert result.status == ResultStatus.CRITICAL_ESCALATED
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.CRITICAL_ESCALATED.value
    assert response["escalationPath"] == "MARK_CRITICAL"

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, Escalation)
    assert added.resultId == RESULT_ID
    assert added.escalatedBy == SUPERVISOR_ID
    assert added.escalationPath == "MARK_CRITICAL"
    assert added.escalationNote == "Urgent"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalateResultRejectsInvalidEscalationPath():
    """An unrecognized escalation_path is rejected before any DB access —
    the same validation the pre-port Supabase service performed.
    """
    db = _makeDbMock(getSideEffect=[])

    _service = ResultReviewService(db=db)
    with pytest.raises(UnprocessableException) as excInfo:
        await _service.escalateResult(
            RESULT_ID, SUPERVISOR_ID, escalationPath="NOT_A_REAL_PATH", escalationNote=None
        )
    assert excInfo.value.errorCode == "INVALID_ESCALATION_PATH"
    db.get.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_approveResultRejectsResultNotPending():
    """approve/return/escalate all share _require_pending — a result that
    isn't PENDING_SUPERVISOR_APPROVAL (e.g. already APPROVED) is rejected.
    """
    result = _makeResult(status=ResultStatus.APPROVED)
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    with pytest.raises(ConflictException) as excInfo:
        await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ── Regression test: spatial_annotations write-then-drop bug ──────────────


def _makeScalarsResult(items: list) -> MagicMock:
    """A db.execute() return value shaped for `.scalars().all()`."""
    executeResult = MagicMock()
    executeResult.scalars.return_value.all.return_value = items
    return executeResult


def _makeScalarOneResult(item) -> MagicMock:
    """A db.execute() return value shaped for `.scalar_one_or_none()`."""
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = item
    return executeResult


@pytest.mark.asyncio
async def test_annotateResultPersistsAndRoundTripsSpatialAnnotations():
    """Regression test for the bug where save_annotation accepted
    spatial_annotations but never assigned it onto the ResultReview object
    it saved, so the value was silently dropped instead of persisted.

    This fails against the pre-fix code: asserting on the actual object
    passed to db.add (not just that the call succeeded) catches a missing
    assignment that a "did it raise?" test would miss entirely. It also
    checks the read side: get_full_result must surface the same value back,
    not just save_annotation's own echoed response.
    """
    payload = [
        {"x": 120, "y": 340, "label": "cast_cluster"},
        {"x": 55, "y": 90, "label": "crystal"},
    ]

    # ── Write path: no existing review row for this result/supervisor pair ──
    arForWrite = _makeResult()
    writeDb = AsyncMock()
    writeDb.get = AsyncMock(return_value=arForWrite)
    writeDb.execute = AsyncMock(return_value=_makeScalarOneResult(None))
    writeDb.add = MagicMock()
    writeDb.commit = AsyncMock()

    _writeService = ResultReviewService(db=writeDb)
    writeResponse = await _writeService.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        annotationNotes="Possible cast cluster, upper-left quadrant",
        spatialAnnotations=payload,
    )

    writeDb.add.assert_called_once()
    savedReview = writeDb.add.call_args[0][0]
    assert isinstance(savedReview, ResultReview)
    # The actual regression: this attribute was never set on the pre-fix code.
    assert savedReview.spatialAnnotations == payload
    assert writeResponse["spatialAnnotations"] == payload

    # ── Read path: get_full_result must read the same saved object back ──
    savedReview.updatedAt = datetime.now(UTC)

    arForRead = _makeResult()
    arForRead.imageId = None
    readDb = AsyncMock()
    readDb.get = AsyncMock(side_effect=[arForRead, None])  # AnalysisResult, then Specimen (none)
    readDb.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarOneResult(savedReview),  # latest ResultReview
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _readService = ResultReviewService(db=readDb)
    detail = await _readService.getFullResult(RESULT_ID)

    assert detail["spatialAnnotations"] == payload
    assert detail["annotationNotes"] == "Possible cast cluster, upper-left quadrant"


@pytest.mark.asyncio
async def test_annotateResultOmittingSpatialAnnotationsPreservesExistingValue():
    """Matches the pre-port behavior: calling save_annotation again without
    spatial_annotations (e.g. to update just the notes) must not clear a
    previously-saved value.
    """
    ar = _makeResult()
    existingReview = MagicMock(spec=ResultReview)
    existingReview.spatialAnnotations = [{"x": 1, "y": 2, "label": "prior"}]
    existingReview.annotationNotes = "old notes"

    db = AsyncMock()
    db.get = AsyncMock(return_value=ar)
    db.execute = AsyncMock(return_value=_makeScalarOneResult(existingReview))
    db.commit = AsyncMock()

    _service = ResultReviewService(db=db)
    await _service.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        annotationNotes="updated notes only",
        spatialAnnotations=None,
    )

    assert existingReview.annotationNotes == "updated notes only"
    # Not overwritten with None just because this call didn't supply a value.
    assert existingReview.spatialAnnotations == [{"x": 1, "y": 2, "label": "prior"}]


# ── getFullResult (previously entirely untested — flagged as a gap in this
#    file's own module docstring) ────────────────────────────────────────

def _makeOverride(paramName: str, overriddenAt: datetime) -> MagicMock:
    o = MagicMock(spec=ManualOverride)
    o.overrideId = uuid.uuid4()
    o.parameterName = paramName
    o.originalAiValue = "10"
    o.correctedValue = "12"
    o.rationale = "recount"
    o.overriddenAt = overriddenAt
    return o


@pytest.mark.asyncio
async def test_getFullResultRaisesNotFoundForMissingResult():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    _service = ResultReviewService(db=db)
    with pytest.raises(NotFoundException) as excInfo:
        await _service.getFullResult(RESULT_ID)
    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_getFullResultAssemblesDetailWithoutPatientOrOverrides():
    """Baseline assembly with no patient_uid on the specimen (so the Patient
    lookup — and its known `.sex` gap, see the dedicated test below — is
    never reached), no image, no medtech, no overrides, no annotation, no
    smart diagnosis output.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None
    ar.confirmedAt = None
    ar.aiFindings = {"RBC": 12}
    ar.flaggedAnomalies = {}
    ar.particleClasses = {}
    ar.modelVersion = "mvp-v1.0"
    ar.smartDiagnosisUnavailable = False

    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])  # AnalysisResult, then Specimen
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),       # manual_overrides
            _makeScalarOneResult(None),   # latest ResultReview
            _makeScalarOneResult(None),   # smart_diagnosis_output
        ]
    )

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    assert detail["resultId"] == RESULT_ID
    assert detail["manualOverrides"] == []
    assert detail["medtechName"] == ""
    assert detail["imageUrl"] is None
    assert detail["smartDiagnosisUnavailable"] is True  # no attached output
    assert detail["confirmationNotes"] is None  # documented schema-drift field


@pytest.mark.asyncio
async def test_getFullResultOrdersManualOverridesByOverriddenAt():
    """Regression guard: the manual_overrides query must sort by
    overridden_at. Without an explicit ORDER BY, Postgres does not
    guarantee insertion order on a plain SELECT, so the supervisor's
    override history could render out of sequence.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None
    ar.aiFindings = {}
    ar.flaggedAnomalies = {}
    ar.particleClasses = {}
    ar.modelVersion = "mvp-v1.0"
    ar.smartDiagnosisUnavailable = False

    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None

    overrides = [
        _makeOverride("RBC", datetime(2026, 1, 1, tzinfo=UTC)),
        _makeOverride("WBC", datetime(2026, 1, 2, tzinfo=UTC)),
    ]

    capturedStatements = []

    async def _executeSideEffect(stmt):
        capturedStatements.append(stmt)
        if len(capturedStatements) == 1:
            return _makeScalarsResult(overrides)
        return _makeScalarOneResult(None)

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(side_effect=_executeSideEffect)

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    assert [o["parameterName"] for o in detail["manualOverrides"]] == ["RBC", "WBC"]
    overridesStmt = capturedStatements[0]
    assert "ORDER BY manual_overrides.overridden_at" in str(overridesStmt)


@pytest.mark.asyncio
async def test_getFullResultPatientSexRaisesAttributeErrorKnownGap():
    """Documents a known, already-flagged gap (changelog.md, "Pyright scan"
    entry: "Patient model has no sex column, but it's read as .sex in 4
    places... deciding whether to add the column or remove the reads is a
    product call, not something to guess at here") — not fixed in this
    change, per that same standing policy. This test locks in the *current*
    (broken) behavior so a silent, accidental fix doesn't go unnoticed
    either: if this starts failing, someone made the product decision and
    this test should be updated to assert the real value instead.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None

    specimen = _makeSpecimen()
    specimen.patientUid = "PT-001"
    specimen.medtechId = None

    patient = MagicMock(spec=Patient)  # spec= enforces the real model's attribute set
    patient.firstName = None
    patient.lastName = None
    patient.dateOfBirth = None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(return_value=_makeScalarOneResult(patient))  # Patient lookup

    _service = ResultReviewService(db=db)
    with pytest.raises(AttributeError):
        await _service.getFullResult(RESULT_ID)


# ── getSmartDiagnosis (module-level function; Supabase-backed, not SQLAlchemy) ──

class _FakeSupabaseQuery:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    async def execute(self):
        return SimpleNamespace(data=self._rows)


class _FakeSupabaseForSmartDiagnosis:
    def __init__(self, analysisRows: list[dict], outputRows: list[dict]):
        self._analysisRows = analysisRows
        self._outputRows = outputRows

    def table(self, name: str):
        if name == "analysis_results":
            return _FakeSupabaseQuery(self._analysisRows)
        if name == "smart_diagnosis_outputs":
            return _FakeSupabaseQuery(self._outputRows)
        return _FakeSupabaseQuery([])


@pytest.mark.asyncio
async def test_getSmartDiagnosisNotFoundRaises404():
    fakeSb = _FakeSupabaseForSmartDiagnosis(analysisRows=[], outputRows=[])
    with patch("src.services.result_review_service.supabase", fakeSb):
        with pytest.raises(Exception) as excInfo:
            await getSmartDiagnosis(str(RESULT_ID))
    assert getattr(excInfo.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_getSmartDiagnosisPrefersAuthoritativeOutputsTable():
    analysisRows = [{
        "result_id": str(RESULT_ID),
        "smart_diagnosis_unavailable": False,
        "smart_diagnosis": {"gout": {"level": "LOW"}},  # denormalized fallback, should be ignored
    }]
    outputRows = [{
        "output_id": str(uuid.uuid4()),
        "status": "ATTACHED",
        "gout_score": "HIGH",
        "gn_score": "MODERATE",
        "nephro_score": "LOW",
        "no_significant_indicators": False,
        "evidence_map": {"gout": ["uric_acid_crystals"]},
        "engine_version": "mvp-v1.0",
    }]
    fakeSb = _FakeSupabaseForSmartDiagnosis(analysisRows=analysisRows, outputRows=outputRows)

    with patch("src.services.result_review_service.supabase", fakeSb):
        result = await getSmartDiagnosis(str(RESULT_ID))

    assert result["status"] == "ATTACHED"
    assert result["gout_score"] == "HIGH"  # from the authoritative table, not the JSONB fallback


@pytest.mark.asyncio
async def test_getSmartDiagnosisFallsBackToDenormalizedJsonb():
    analysisRows = [{
        "result_id": str(RESULT_ID),
        "smart_diagnosis_unavailable": False,
        "smart_diagnosis": {
            "gout": {"level": "HIGH"},
            "glomerulonephritis": {"level": "LOW"},
            "nephrolithiasis": {"level": "MODERATE"},
            "no_significant_indicators": False,
            "engine_version": "mvp-v1.0",
        },
    }]
    fakeSb = _FakeSupabaseForSmartDiagnosis(analysisRows=analysisRows, outputRows=[])

    with patch("src.services.result_review_service.supabase", fakeSb):
        result = await getSmartDiagnosis(str(RESULT_ID))

    assert result["status"] == "ATTACHED"
    assert result["gout_score"] == "HIGH"
    assert result["gn_score"] == "LOW"
    assert result["nephro_score"] == "MODERATE"


@pytest.mark.asyncio
async def test_getSmartDiagnosisNoDataReturnsFlaggedUnavailable():
    analysisRows = [{
        "result_id": str(RESULT_ID),
        "smart_diagnosis_unavailable": True,
        "smart_diagnosis": None,
    }]
    fakeSb = _FakeSupabaseForSmartDiagnosis(analysisRows=analysisRows, outputRows=[])

    with patch("src.services.result_review_service.supabase", fakeSb):
        result = await getSmartDiagnosis(str(RESULT_ID))

    assert result == {"result_id": str(RESULT_ID), "status": "FLAGGED_UNAVAILABLE"}
