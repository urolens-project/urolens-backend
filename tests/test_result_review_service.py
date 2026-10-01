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
call succeeding) and passes against the fix. Beyond that, get_pending,
get_approved_today, and get_escalated are only covered for the one field
UROLENS-148 added (sampleUid) and the rejected-specimen filter above —
their pagination, ordering, and non-sampleUid fields remain untested.
get_supervisor_stats itself is covered separately in
tests/integration/test_supervisor_stats.py.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select

from src.core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
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
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000033")


def _makeResult(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.medtechId = MEDTECH_ID
    return result


def _makeSpecimen() -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.completedAt = None
    return specimen


def _makeDbMock(getSideEffect: list) -> AsyncMock:
    """db.get(Model, id) returns the next item in get_side_effect, in call
    order. db.execute defaults to a winning conditional UPDATE (rowcount=1),
    matching the non-race happy path — a test simulating a lost race
    overrides db.execute itself.
    """
    db = AsyncMock()
    db.get = AsyncMock(side_effect=getSideEffect)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    executeResult = MagicMock()
    executeResult.rowcount = 1
    db.execute = AsyncMock(return_value=executeResult)
    return db


def _compiledTransitionStatement(db: AsyncMock) -> str:
    """The compiled SQL of the conditional-UPDATE transition statement
    approve/return/escalate issue via `_transitionIfPending` — the literal
    replacement for asserting on a mutated ORM attribute, since the new
    race-safe implementation no longer sets `.status` directly on the
    in-memory object.
    """
    stmt = db.execute.call_args[0][0]
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_approveResultTransitionsStatusAndCompletesSpecimen():
    """Happy path: approving a pending result records the approval, moves the
    result to APPROVED, and marks its specimen COMPLETED so it leaves the
    medtech's active queue.
    """
    result = _makeResult()
    specimen = _makeSpecimen()
    db = _makeDbMock(getSideEffect=[result, specimen])

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    response = await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes="Looks good")

    updateSql = _compiledTransitionStatement(db)
    assert "SET status='APPROVED'" in updateSql
    assert "analysis_results.status = 'PENDING_SUPERVISOR_APPROVAL'" in updateSql
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
    notifService = AsyncMock()

    _service = ResultReviewService(db=db, notifService=notifService)
    response = await _service.returnResult(RESULT_ID, SUPERVISOR_ID, reason="Blurry image")

    updateSql = _compiledTransitionStatement(db)
    assert "SET status='RETURNED_FOR_CORRECTION'" in updateSql
    assert "analysis_results.status = 'PENDING_SUPERVISOR_APPROVAL'" in updateSql
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.RETURNED_FOR_CORRECTION.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultReturn)
    assert added.resultId == RESULT_ID
    assert added.returnedBy == SUPERVISOR_ID
    assert added.reason == "Blurry image"
    db.commit.assert_awaited_once()

    notifService.notify.assert_awaited_once_with(
        userId=MEDTECH_ID,
        message="A result was returned for correction: Blurry image",
        notificationType="RESULT_RETURNED",
        entityId=RESULT_ID,
    )


@pytest.mark.asyncio
async def test_escalateResultTransitionsStatusAndRecordsPath():
    """Happy path: escalating a pending result records the escalation path
    and moves the result to CRITICAL_ESCALATED.
    """
    result = _makeResult()
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    response = await _service.escalateResult(
        RESULT_ID, SUPERVISOR_ID, escalationPath="MARK_CRITICAL", escalationNote="Urgent"
    )

    updateSql = _compiledTransitionStatement(db)
    assert "SET status='CRITICAL_ESCALATED'" in updateSql
    assert "analysis_results.status = 'PENDING_SUPERVISOR_APPROVAL'" in updateSql
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

    _service = ResultReviewService(db=db, notifService=AsyncMock())
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

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    with pytest.raises(ConflictException) as excInfo:
        await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approveResultRefusesResultOfRejectedSpecimen():
    """A rejected specimen's result must never be approved, even if a row
    reached the queue before rejection was blocked after confirmation.
    """
    result = _makeResult()
    specimen = _makeSpecimen()
    specimen.status = "REJECTED"
    db = _makeDbMock(getSideEffect=[result, specimen])

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    with pytest.raises(ConflictException) as excInfo:
        await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)

    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"
    assert result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert specimen.status == "REJECTED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_pendingQueueAndStatsExcludeRejectedSpecimens():
    """The supervisor's pending list, its total and the dashboard badge must
    all leave out results whose specimen was rejected.
    """
    executed: list[str] = []

    async def _execute(stmt):
        executed.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        emptyResult = MagicMock()
        emptyResult.scalar_one.return_value = 0
        emptyResult.scalars.return_value.all.return_value = []
        return emptyResult

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    _service = ResultReviewService(db=db, notifService=AsyncMock())

    await _service.getPending(page=1, pageSize=10)
    await _service.getSupervisorStats()

    pendingStatements = [s for s in executed if "PENDING_SUPERVISOR_APPROVAL" in s]
    assert pendingStatements, "expected pending-approval queries to have run"
    for sql in pendingStatements:
        assert "specimens.status != 'REJECTED'" in sql


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
    writeDb.execute = AsyncMock(
        side_effect=[_makeScalarOneResult(arForWrite), _makeScalarOneResult(None)]  # result, existing review
    )
    writeDb.add = MagicMock()
    writeDb.commit = AsyncMock()

<<<<<<< HEAD
    _writeService = ResultReviewService(db=writeDb, notifService=AsyncMock())
=======
    _writeService = ResultReviewService(db=writeDb, auditLogger=MagicMock(record=AsyncMock()))
>>>>>>> 62167e517b74744cf23ec401c7170898c16df18c
    writeResponse = await _writeService.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        callerRole="SUPERVISOR",
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
            _makeScalarsResult([savedReview]),  # reviews (one, by the supervisor)
            _makeScalarsResult([]),  # reviewer role lookup (unresolved -> "")
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _readService = ResultReviewService(db=readDb, notifService=AsyncMock())
    detail = await _readService.getFullResult(RESULT_ID)

    [annotation] = detail["annotations"]
    assert annotation["spatialAnnotations"] == payload
    assert annotation["annotationNotes"] == "Possible cast cluster, upper-left quadrant"
    assert annotation["reviewedBy"] == SUPERVISOR_ID


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
    db.execute = AsyncMock(
        side_effect=[_makeScalarOneResult(ar), _makeScalarOneResult(existingReview)]  # result, existing review
    )
    db.commit = AsyncMock()

<<<<<<< HEAD
    _service = ResultReviewService(db=db, notifService=AsyncMock())
=======
    _service = ResultReviewService(db=db, auditLogger=MagicMock(record=AsyncMock()))
>>>>>>> 62167e517b74744cf23ec401c7170898c16df18c
    await _service.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        callerRole="SUPERVISOR",
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

    _service = ResultReviewService(db=db, notifService=AsyncMock())
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
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([]),  # reviews (none)
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    detail = await _service.getFullResult(RESULT_ID)

    assert detail["resultId"] == RESULT_ID
    assert detail["manualOverrides"] == []
    assert detail["annotations"] == []
    assert detail["medtechName"] == ""
    assert detail["imageUrl"] is None
    assert detail["smartDiagnosisUnavailable"] is True  # no attached output
    assert detail["confirmationNotes"] is None  # documented schema-drift field


@pytest.mark.asyncio
async def test_getFullResultGivesTheImageAsAShortLivedSignedUrlNotAPublicOne():
    """SEC-0b: the microscopy bucket is private, so the supervisor's image
    link must be a signed URL — a permanent /object/public/ link would 400
    against a private bucket and, while the bucket was public, exposed the
    image to anyone holding the link.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = uuid.uuid4()
    ar.confirmedAt = None
    ar.aiFindings = {}
    ar.flaggedAnomalies = {}
    ar.particleClasses = {}
    ar.modelVersion = "mvp-v1.0"
    ar.smartDiagnosisUnavailable = False

    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None

    storageKey = f"specimens/{specimen.specimenId}/images/{ar.imageId}.jpg"
    image = SimpleNamespace(storageKey=storageKey)
    signedUrl = f"https://example.supabase.co/storage/v1/object/sign/microscopy/{storageKey}?token=t"

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen, image])  # AnalysisResult, Specimen, Image
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([]),  # reviews (none)
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )
    bucket = MagicMock()
    bucket.create_signed_url = AsyncMock(return_value={"signedURL": signedUrl, "signedUrl": signedUrl})
    fakeSb = MagicMock()
    fakeSb.storage.from_.return_value = bucket

    with patch("src.core.storage.supabase", fakeSb):
        detail = await ResultReviewService(db=db).getFullResult(RESULT_ID)

    assert detail["imageUrl"] == signedUrl
    assert "/object/public/" not in detail["imageUrl"]
    bucket.create_signed_url.assert_awaited_once()
    assert bucket.create_signed_url.await_args.args[0] == storageKey


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
    specimen.patientName = None

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

    _service = ResultReviewService(db=db, notifService=AsyncMock())
    detail = await _service.getFullResult(RESULT_ID)

    assert [o["parameterName"] for o in detail["manualOverrides"]] == ["RBC", "WBC"]
    overridesStmt = capturedStatements[0]
    assert "ORDER BY manual_overrides.overridden_at" in str(overridesStmt)


@pytest.mark.asyncio
async def test_getFullResultResolvesWhoMadeEachOverride() -> None:
    """UROLENS-149: the override-history list must say who made each
    correction, not just what/when — resolved via a batched User lookup
    (same pattern as `medtechName` elsewhere in this method). A user id with
    no matching row falls back to `""`, mirroring `medtechName`'s fallback.
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
    specimen.patientName = None

    knownUserId = uuid.uuid4()
    unknownUserId = uuid.uuid4()

    overrideByKnownUser = _makeOverride("RBC", datetime(2026, 1, 1, tzinfo=UTC))
    overrideByKnownUser.overriddenBy = knownUserId
    overrideByUnknownUser = _makeOverride("WBC", datetime(2026, 1, 2, tzinfo=UTC))
    overrideByUnknownUser.overriddenBy = unknownUserId

    knownUser = MagicMock(spec=User)
    knownUser.userId = knownUserId
    knownUser.username = "jdelacruz"

    capturedStatements: list[Select] = []

    async def _executeSideEffect(stmt: Select) -> MagicMock:
        capturedStatements.append(stmt)
        if len(capturedStatements) == 1:
            return _makeScalarsResult([overrideByKnownUser, overrideByUnknownUser])
        if len(capturedStatements) == 2:
            return _makeScalarsResult([knownUser])  # unknownUserId has no matching row
        return _makeScalarOneResult(None)

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(side_effect=_executeSideEffect)

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    overridesById = {o["overriddenBy"]: o for o in detail["manualOverrides"]}
    assert overridesById[knownUserId]["overriddenByName"] == "jdelacruz"
    assert overridesById[unknownUserId]["overriddenByName"] == ""


@pytest.mark.asyncio
async def test_getFullResultReturnsPatientSex() -> None:
    """`Patient.sex` is a real column (src/models/patient.py) and
    `getFullResult` reads it correctly.

    This test used to assert the opposite — that reading `.sex` raised
    `AttributeError` as a "documented, known gap". That was never actually
    about `.sex`: the test stubbed `db.execute` with one fixed return value
    standing in for four different queries `getFullResult` makes (Patient,
    ManualOverride, ResultReview, SmartDiagnosisOutput), so the ResultReview
    query wrongly got the Patient mock back, and `review.annotationNotes`
    (not `.sex`) was what actually raised. Fixed here with a proper
    side_effect per query, matching the real call order.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None

    specimen = _makeSpecimen()
    specimen.patientUid = "PT-001"
    specimen.medtechId = None
    specimen.patientName = None

    patient = MagicMock(spec=Patient)  # spec= enforces the real model's attribute set
    patient.firstName = None
    patient.lastName = None
    patient.dateOfBirth = None
    patient.sex = "FEMALE"

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarOneResult(patient),  # Patient lookup
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([]),  # reviews (none)
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

<<<<<<< HEAD
    _service = ResultReviewService(db=db, notifService=AsyncMock())
    with pytest.raises(AttributeError):
        await _service.getFullResult(RESULT_ID)
=======
    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    assert detail["patientSex"] == "FEMALE"


@pytest.mark.asyncio
async def test_getFullResultIncludesSampleUidFromSpecimen() -> None:
    """UROLENS-150: the detail view must surface the specimen's human-facing
    sampleUid (e.g. SMP-...), not just its internal UUID — same fix as
    UROLENS-148's queue-list endpoints, applied here to getFullResult.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None

    specimen = _makeSpecimen()
    specimen.sampleUid = "SMP-20260928-12345"
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([]),  # reviews (none)
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    assert detail["sampleUid"] == "SMP-20260928-12345"
    assert detail["sampleUid"] != str(SPECIMEN_ID)


@pytest.mark.asyncio
async def test_getFullResultSurfacesEachReviewersAnnotationSeparately() -> None:
    """A MedTech's and a Supervisor's annotations on the same result are
    independent `ResultReview` rows (keyed by resultId + reviewedBy) — both
    must be surfaced, attributed by role, not collapsed to whichever was
    saved most recently.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None

    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None

    medtechId = uuid.uuid4()
    medtechReview = MagicMock(spec=ResultReview)
    medtechReview.reviewedBy = medtechId
    medtechReview.annotationNotes = "Looks like a cast cluster"
    medtechReview.spatialAnnotations = None
    medtechReview.updatedAt = datetime(2026, 1, 1, tzinfo=UTC)

    supervisorReview = MagicMock(spec=ResultReview)
    supervisorReview.reviewedBy = SUPERVISOR_ID
    supervisorReview.annotationNotes = "Confirmed, escalating"
    supervisorReview.spatialAnnotations = [{"id": "a1", "x": 1, "y": 2, "particleType": "urinary_casts"}]
    supervisorReview.updatedAt = datetime(2026, 1, 2, tzinfo=UTC)

    medtechUser = MagicMock(spec=User)
    medtechUser.userId = medtechId
    medtechUser.role = "MEDTECH"
    supervisorUser = MagicMock(spec=User)
    supervisorUser.userId = SUPERVISOR_ID
    supervisorUser.role = "SUPERVISOR"

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([medtechReview, supervisorReview]),  # reviews, oldest first
            _makeScalarsResult([medtechUser, supervisorUser]),  # reviewer role lookup
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    annotationsByReviewer = {a["reviewedBy"]: a for a in detail["annotations"]}
    assert len(annotationsByReviewer) == 2

    medtechAnnotation = annotationsByReviewer[medtechId]
    assert medtechAnnotation["reviewerRole"] == "MEDTECH"
    assert medtechAnnotation["annotationNotes"] == "Looks like a cast cluster"
    assert medtechAnnotation["spatialAnnotations"] is None

    supervisorAnnotation = annotationsByReviewer[SUPERVISOR_ID]
    assert supervisorAnnotation["reviewerRole"] == "SUPERVISOR"
    assert supervisorAnnotation["annotationNotes"] == "Confirmed, escalating"
    assert supervisorAnnotation["spatialAnnotations"] == [
        {"id": "a1", "x": 1, "y": 2, "particleType": "urinary_casts"}
    ]


@pytest.mark.asyncio
async def test_getFullResultUnknownReviewerFallsBackToEmptyRole() -> None:
    """A `reviewedBy` id with no matching `User` row falls back to `""`,
    mirroring `overriddenByName`'s fallback for the same situation.
    """
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None

    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None

    unknownReviewerId = uuid.uuid4()
    review = MagicMock(spec=ResultReview)
    review.reviewedBy = unknownReviewerId
    review.annotationNotes = "Notes from a deleted account"
    review.spatialAnnotations = None
    review.updatedAt = datetime(2026, 1, 1, tzinfo=UTC)

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarsResult([review]),
            _makeScalarsResult([]),  # reviewer role lookup finds nothing
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _service = ResultReviewService(db=db)
    detail = await _service.getFullResult(RESULT_ID)

    [annotation] = detail["annotations"]
    assert annotation["reviewerRole"] == ""
>>>>>>> 62167e517b74744cf23ec401c7170898c16df18c


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
    assert result["goutScore"] == "HIGH"  # from the authoritative table, not the JSONB fallback


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
    assert result["goutScore"] == "HIGH"
    assert result["gnScore"] == "LOW"
    assert result["nephroScore"] == "MODERATE"


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

    assert result == {"resultId": str(RESULT_ID), "status": "FLAGGED_UNAVAILABLE"}


# ── UROLENS-148: sampleUid on the three supervisor queue endpoints ─────────


def _makeQueueSpecimen(sampleUid: str = "SMP-20260928-12345") -> Specimen:
    """A specimen with no patient/medtech linkage, so `_batchPatientContext`
    skips the patient and user lookups — isolates these tests to the one
    thing they check.
    """
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.sampleUid = sampleUid
    specimen.patientUid = None
    specimen.patientName = None
    specimen.medtechId = None
    specimen.status = "ASSIGNED"
    return specimen


def _makeCountResult(n: int) -> MagicMock:
    executeResult = MagicMock()
    executeResult.scalar_one.return_value = n
    return executeResult


@pytest.mark.asyncio
async def test_getPendingIncludesSampleUidFromSpecimen():
    """UROLENS-148: the pending list must surface the specimen's human-facing
    sampleUid (e.g. SMP-...), not the specimen's raw UUID.
    """
    ar = _makeResult()
    ar.confirmedAt = datetime(2026, 9, 28, tzinfo=UTC)
    specimen = _makeQueueSpecimen()

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _makeCountResult(1),
            _makeScalarsResult([ar]),
            _makeScalarsResult([specimen]),
        ]
    )
    _service = ResultReviewService(db=db)

    response = await _service.getPending(page=1, pageSize=10)

    assert response["items"][0]["sampleUid"] == "SMP-20260928-12345"
    assert response["items"][0]["sampleUid"] != str(SPECIMEN_ID)


@pytest.mark.asyncio
async def test_getApprovedTodayIncludesSampleUidFromSpecimen():
    """UROLENS-148: same guarantee for the approved-today list."""
    ar = _makeResult(status=ResultStatus.APPROVED)
    approval = MagicMock(spec=ResultApproval)
    approval.resultId = RESULT_ID
    approval.approvedAt = datetime(2026, 9, 28, tzinfo=UTC)
    specimen = _makeQueueSpecimen()

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _makeCountResult(1),
            _makeScalarsResult([approval]),
            _makeScalarsResult([ar]),
            _makeScalarsResult([specimen]),
        ]
    )
    _service = ResultReviewService(db=db)

    response = await _service.getApprovedToday(page=1, pageSize=10)

    assert response["items"][0]["sampleUid"] == "SMP-20260928-12345"
    assert response["items"][0]["sampleUid"] != str(SPECIMEN_ID)


@pytest.mark.asyncio
async def test_getEscalatedIncludesSampleUidFromSpecimen():
    """UROLENS-148: same guarantee for the escalated list."""
    ar = _makeResult(status=ResultStatus.CRITICAL_ESCALATED)
    ar.updatedAt = datetime(2026, 9, 28, tzinfo=UTC)
    specimen = _makeQueueSpecimen()

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _makeCountResult(1),
            _makeScalarsResult([ar]),
            _makeScalarsResult([]),  # no Escalation row needed for this assertion
            _makeScalarsResult([specimen]),
        ]
    )
    _service = ResultReviewService(db=db)

    response = await _service.getEscalated(page=1, pageSize=10)

    assert response["items"][0]["sampleUid"] == "SMP-20260928-12345"
    assert response["items"][0]["sampleUid"] != str(SPECIMEN_ID)
