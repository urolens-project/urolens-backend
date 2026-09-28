"""Unit tests — the MedTech result review screen and annotation access (UROLENS-226).

`GET /results/{id}` (`getFullResult`) is the mobile review source: it carries the
supervisor's latest return reason, and a MedTech gets no patient name (the app
shows the patient code only). `PATCH /results/{id}/annotate` (`saveAnnotation`)
follows the manual-override access rules: MedTech ownership under the specimen
lock, status rules per role, and an audit row committed with the change. DB is
mocked.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select

from src.core.encryption import encryptPii
from src.core.exceptions import (
    ConflictException,
    ForbiddenException,
    NotFoundException,
    UnprocessableException,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.patient import Patient
from src.models.result_return import ResultReturn
from src.models.result_review import ResultReview
from src.models.specimen import Specimen
from src.services import result_review_service
from src.services.result_review_service import ResultReviewService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e3")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e4")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e5")


def _makeResult(status: str = ResultStatus.PENDING_CONFIRM) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.imageId = None
    result.confirmedAt = None
    result.aiFindings = {"RBC": 12}
    result.flaggedAnomalies = {}
    result.particleClasses = {}
    result.modelVersion = "mvp-v1.0"
    result.smartDiagnosisUnavailable = False
    return result


def _makeSpecimen(medtechId: uuid.UUID | None = MEDTECH_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    specimen.patientUid = "PAT-000001"
    specimen.patientName = None
    return specimen


def _patient() -> SimpleNamespace:
    # SimpleNamespace, not spec=Patient: getFullResult reads `.sex`, a known gap
    # (see test_getFullResultPatientSexRaisesAttributeErrorKnownGap).
    return SimpleNamespace(
        firstName=encryptPii("Juan"), lastName=encryptPii("Dela Cruz"), dateOfBirth=None, sex="M"
    )


def _makeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


# ── getFullResult: return reason and patient name ─────────────────────────────

def _detailDb(result: AnalysisResult, specimen: Specimen, returnReason: str | None = None) -> tuple[AsyncMock, list]:
    """A session answering each getFullResult query by the entity it selects."""
    medtech = SimpleNamespace(username="medtech1")
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[result, specimen, medtech])
    returnQueries: list = []

    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        executeResult = MagicMock()
        executeResult.scalars.return_value.all.return_value = []
        answers = {Patient: _patient(), ResultReturn: returnReason, ManualOverride: None, ResultReview: None}
        if entity is ResultReturn:
            returnQueries.append(stmt)
        executeResult.scalar_one_or_none.return_value = answers.get(entity)
        return executeResult

    db.execute = AsyncMock(side_effect=_execute)
    return db, returnQueries


@pytest.mark.asyncio
async def test_detailOfAReturnedResultCarriesTheLatestReturnReason() -> None:
    db, returnQueries = _detailDb(
        _makeResult(ResultStatus.RETURNED_FOR_CORRECTION), _makeSpecimen(), returnReason="Recount the casts"
    )

    detail = await ResultReviewService(db=db, auditLogger=_makeAuditLogger()).getFullResult(
        RESULT_ID, viewerId=MEDTECH_ID, viewerRole="MEDTECH"
    )

    assert detail["returnReason"] == "Recount the casts"
    [stmt] = returnQueries
    sql = str(stmt)
    assert "ORDER BY result_returns.returned_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ResultStatus.PENDING_CONFIRM, ResultStatus.PENDING_SUPERVISOR_APPROVAL, ResultStatus.APPROVED],
)
async def test_detailOfAResultThatIsNotReturnedHasNoReturnReason(status: ResultStatus) -> None:
    # An old return row must not resurface once the result was resubmitted.
    db, returnQueries = _detailDb(_makeResult(status), _makeSpecimen(), returnReason="stale reason")

    detail = await ResultReviewService(db=db, auditLogger=_makeAuditLogger()).getFullResult(
        RESULT_ID, viewerId=MEDTECH_ID, viewerRole="MEDTECH"
    )

    assert detail["returnReason"] is None
    assert returnQueries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["MEDTECH", "medtech"])
async def test_medtechDetailHasThePatientCodeButNoNameAndNeverDecryptsIt(role: str) -> None:
    db, _ = _detailDb(_makeResult(), _makeSpecimen())
    service = ResultReviewService(db=db, auditLogger=_makeAuditLogger())

    with patch.object(
        result_review_service, "decryptStoredPii", wraps=result_review_service.decryptStoredPii
    ) as decryptSpy:
        detail = await service.getFullResult(RESULT_ID, viewerId=MEDTECH_ID, viewerRole=role)

    assert detail["patientName"] is None
    assert detail["patientUid"] == "PAT-000001"
    # Only the date of birth (None here, for the age) is decrypted — never a name.
    assert [call.args[0] for call in decryptSpy.call_args_list] == [None]


@pytest.mark.asyncio
async def test_supervisorDetailStillHasThePatientName() -> None:
    db, _ = _detailDb(_makeResult(ResultStatus.PENDING_SUPERVISOR_APPROVAL), _makeSpecimen())

    detail = await ResultReviewService(db=db, auditLogger=_makeAuditLogger()).getFullResult(
        RESULT_ID, viewerId=SUPERVISOR_ID, viewerRole="SUPERVISOR"
    )

    assert detail["patientName"] == "Juan Dela Cruz"


# ── saveAnnotation: ownership, status rules, audit ────────────────────────────

def _annotateDb(
    result: AnalysisResult | None,
    specimen: Specimen | None = None,
    existingReview: ResultReview | None = None,
) -> AsyncMock:
    """Result reads (initial and fresh) return `result`; the review lookup `existingReview`."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = existingReview if entity is ResultReview else result
        return executeResult

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


async def _annotate(db: AsyncMock, userId: uuid.UUID, role: str, auditLogger: MagicMock | None = None) -> dict:
    service = ResultReviewService(db=db, auditLogger=auditLogger or _makeAuditLogger())
    return await service.saveAnnotation(
        resultId=RESULT_ID,
        userId=userId,
        callerRole=role,
        annotationNotes="Possible cast cluster",
        spatialAnnotations=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION])
async def test_medtechAnnotatesTheirOwnEditableResultUnderTheSpecimenLock(status: ResultStatus) -> None:
    db = _annotateDb(_makeResult(status), _makeSpecimen())

    response = await _annotate(db, MEDTECH_ID, "MEDTECH")

    assert response["annotationNotes"] == "Possible cast cluster"
    db.get.assert_awaited_once_with(Specimen, SPECIMEN_ID, with_for_update=True)
    # The status is re-read after the lock, not trusted from the first read.
    freshReads = [
        c.args[0] for c in db.execute.await_args_list
        if c.args[0].get_execution_options().get("populate_existing")
    ]
    assert len(freshReads) == 1
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("assignedTo", [OTHER_MEDTECH_ID, None])
async def test_medtechCannotAnnotateAnotherMedtechsOrAnUnassignedSpecimen(assignedTo: uuid.UUID | None) -> None:
    # A finalised status too: ownership is checked first, so a non-owner learns nothing.
    db = _annotateDb(_makeResult(ResultStatus.APPROVED), _makeSpecimen(medtechId=assignedTo))
    auditLogger = _makeAuditLogger()

    with pytest.raises(ForbiddenException) as excInfo:
        await _annotate(db, MEDTECH_ID, "MEDTECH", auditLogger)

    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        ResultStatus.CRITICAL_ESCALATED,
        ResultStatus.IMAGE_RETAKE_REQUESTED,
        ResultStatus.FAILED,
    ],
)
async def test_medtechCannotAnnotateOnceTheResultLeftTheirHands(status: ResultStatus) -> None:
    db = _annotateDb(_makeResult(status), _makeSpecimen())

    with pytest.raises(ConflictException) as excInfo:
        await _annotate(db, MEDTECH_ID, "MEDTECH")

    assert excInfo.value.status_code == 409
    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_supervisorAnnotatesWhileReviewingWithoutAnOwnershipLookup() -> None:
    db = _annotateDb(_makeResult(ResultStatus.PENDING_SUPERVISOR_APPROVAL))

    await _annotate(db, SUPERVISOR_ID, "SUPERVISOR")

    db.get.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION, ResultStatus.CRITICAL_ESCALATED],
)
async def test_supervisorCannotAnnotateOutsideTheirReview(status: ResultStatus) -> None:
    db = _annotateDb(_makeResult(status))

    with pytest.raises(ConflictException) as excInfo:
        await _annotate(db, SUPERVISOR_ID, "SUPERVISOR")

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("userId", "role"), [(MEDTECH_ID, "MEDTECH"), (SUPERVISOR_ID, "SUPERVISOR")])
@pytest.mark.parametrize("status", [ResultStatus.APPROVED, ResultStatus.RELEASED])
async def test_nobodyAnnotatesAFinalisedResult(userId: uuid.UUID, role: str, status: ResultStatus) -> None:
    db = _annotateDb(_makeResult(status), _makeSpecimen())

    with pytest.raises(UnprocessableException) as excInfo:
        await _annotate(db, userId, role)

    assert excInfo.value.status_code == 422
    assert excInfo.value.errorCode == "RESULT_ALREADY_FINALISED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_annotatingAMissingResultIsNotFound() -> None:
    db = _annotateDb(None)

    with pytest.raises(NotFoundException) as excInfo:
        await _annotate(db, SUPERVISOR_ID, "SUPERVISOR")

    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_annotationIsAuditedInItsTransactionWithoutTheNoteText() -> None:
    db = _annotateDb(_makeResult(ResultStatus.RETURNED_FOR_CORRECTION), _makeSpecimen())
    auditLogger = _makeAuditLogger()

    await _annotate(db, MEDTECH_ID, "MEDTECH", auditLogger)

    auditLogger.record.assert_awaited_once()
    kwargs = auditLogger.record.await_args.kwargs
    assert kwargs["eventType"] == "ANNOTATION_SAVED"
    assert kwargs["entityId"] == RESULT_ID
    assert kwargs["userId"] == MEDTECH_ID
    assert kwargs["db"] is db
    assert "Possible cast cluster" not in str(kwargs["detailJson"])
    db.commit.assert_awaited_once()
