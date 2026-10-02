"""Unit tests — PatientResultService (SQLAlchemy AsyncSession implementation)

No tests previously existed for this service (confirmed via full-repo grep
as part of the Supabase-REST → SQLAlchemy migration plan). Covers normal
list/detail behavior plus the bundled status-gate fix: `getResultDetail`
(and therefore the PDF-download route, which calls it internally) must
reject a result that hasn't reached `RELEASED` status, even for its own
owning patient — previously the only checks were existence (404) and
ownership (403), so a patient could see full clinical detail — particle
counts, confirmation notes — for a result still mid-review.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.models.analysis_result import AnalysisResult
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.patient_portal import PatientResultDetailResponse
from src.services.patient_result_service import PatientResultService

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000050")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000051")
OTHER_PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000052")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000053")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000054")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000055")


# ── Row / fixture builders ────────────────────────────────────────────────────

_UNSET = object()


def _makeResultRow(resultId=None, resultStatus="RELEASED", patientId=_UNSET, **overrides):
    row = MagicMock(spec=AnalysisResult)
    row.resultId = resultId or RESULT_ID
    row.status = resultStatus
    row.patientId = PATIENT_ID if patientId is _UNSET else patientId
    row.specimenId = overrides.get("specimenId", SPECIMEN_ID)
    row.releasedAt = overrides.get("releasedAt")
    row.confirmedAt = overrides.get("confirmedAt")
    row.interpretation = overrides.get("interpretation")
    # medtechName is the dead AnalysisResult column (UROLENS-156) — deliberately
    # NOT read by getResultDetail anymore. Defaults to a value that would prove
    # a regression if it were ever read again.
    row.medtechName = overrides.get("medtechName", "WRONG-dead-column-value")
    row.aiFindings = overrides.get("aiFindings", {})
    row.particleClasses = overrides.get("particleClasses", {})
    row.smartDiagnosisUnavailable = overrides.get("smartDiagnosisUnavailable", False)
    return row


def _makeSpecimen(specimenId=None, medtechId=_UNSET) -> MagicMock:
    spec = MagicMock(spec=Specimen)
    spec.specimenId = specimenId or SPECIMEN_ID
    spec.medtechId = MEDTECH_ID if medtechId is _UNSET else medtechId
    return spec


def _makeUser(userId=None, username="jdoe") -> MagicMock:
    user = MagicMock(spec=User)
    user.userId = userId or MEDTECH_ID
    user.username = username
    return user


def _makeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


def _makeResolveOnlyDb(patientId) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = patientId
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _makeDetailDb(patientId, row, getMap: dict | None = None) -> AsyncMock:
    """`db.get(AnalysisResult, _)` returns `row`; any other model/id pair is
    looked up in `getMap` (used for the Specimen/User chain `analyzedBy` now
    resolves through — UROLENS-156).
    """
    resolveResult = MagicMock()
    resolveResult.scalar_one_or_none.return_value = patientId
    db = AsyncMock()
    db.execute = AsyncMock(return_value=resolveResult)

    async def _get(model, id_):
        if model is AnalysisResult:
            return row
        return (getMap or {}).get((model, id_))

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


def _fakeRequest() -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


# ── _resolvePatientId ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolvePatientIdRaises404WhenNoPatientRecord():
    db = _makeResolveOnlyDb(None)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service._resolvePatientId(USER_ID)

    assert excInfo.value.status_code == 404
    assert excInfo.value.errorCode == "PATIENT_NOT_FOUND"


# ── getPatientResults ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_getPatientResultsShowsPlaceholderForUnreleasedRows():
    """A patient's result list must not leak the internal workflow status of
    a not-yet-released result — only whether it's released or still pending.
    """
    resolveResult = MagicMock()
    resolveResult.scalar_one_or_none.return_value = PATIENT_ID

    pendingRow = _makeResultRow(resultStatus="APPROVED", releasedAt=None)
    releasedRow = _makeResultRow(
        resultId=uuid.uuid4(), resultStatus="RELEASED", releasedAt=datetime.now(UTC)
    )
    listResult = MagicMock()
    listResult.scalars.return_value.all.return_value = [pendingRow, releasedRow]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[resolveResult, listResult])
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    items = await service.getPatientResults(USER_ID)

    byId = {item.resultId: item for item in items}
    assert byId[pendingRow.resultId].status == "PENDING"
    assert byId[releasedRow.resultId].status == "RELEASED"


# ── getResultDetail — existence / ownership (unchanged behavior) ─────────────

@pytest.mark.asyncio
async def test_getResultDetailRaises404WhenResultMissing():
    db = _makeDetailDb(PATIENT_ID, None)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert excInfo.value.status_code == 404
    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenNotOwner():
    row = _makeResultRow(patientId=OTHER_PATIENT_ID, resultStatus="RELEASED")
    db = _makeDetailDb(PATIENT_ID, row)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "ACCESS_DENIED"


# ── getResultDetail — status-gate fix (security-relevant) ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "badStatus",
    [
        "PENDING_CONFIRM",
        "PENDING_SUPERVISOR_APPROVAL",
        "APPROVED",
        "RETURNED_FOR_CORRECTION",
        "CRITICAL_ESCALATED",
    ],
)
async def test_getResultDetailRaises403ForOwnedButNotYetReleasedResult(badStatus):
    """A patient must not be able to retrieve full clinical detail for their
    own result before it's RELEASED — this is what closes the PHI-exposure
    gap the migration plan flagged (no status check previously existed).
    """
    row = _makeResultRow(resultStatus=badStatus, patientId=PATIENT_ID)
    db = _makeDetailDb(PATIENT_ID, row)
    auditLogger = _makeAuditLogger()
    service = PatientResultService(db=db, auditLogger=auditLogger)

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "RESULT_NOT_RELEASED"
    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_getResultDetailReturnsFullDetailWhenReleased():
    """A RELEASED result must still return full detail exactly as before."""
    row = _makeResultRow(
        resultStatus="RELEASED",
        patientId=PATIENT_ID,
        aiFindings={"bacteria": 5},
        interpretation="Normal",
        releasedAt=datetime.now(UTC),
    )
    getMap = {
        (Specimen, SPECIMEN_ID): _makeSpecimen(medtechId=MEDTECH_ID),
        (User, MEDTECH_ID): _makeUser(username="jdoe"),
    }
    db = _makeDetailDb(PATIENT_ID, row, getMap)
    auditLogger = _makeAuditLogger()
    service = PatientResultService(db=db, auditLogger=auditLogger)

    result = await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert result.status == "RELEASED"
    assert result.confirmationNotes == "Normal"
    assert result.analyzedBy == "jdoe"
    bacteriaCount = next(p for p in result.particleCounts if p.label == "bacteria")
    assert bacteriaCount.count == 5

    auditLogger.record.assert_awaited_once()
    assert auditLogger.record.call_args.kwargs["eventType"] == "RESULT_VIEWED"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


# ── getResultDetail — analyzedBy / interpretation fixes (UROLENS-156) ────────

@pytest.mark.asyncio
async def test_getResultDetailAnalyzedByComesFromSpecimenMedtechIdNotTheDeadColumn():
    """The real source, matching physician_result_service.getResultDetail and
    ResultReviewService.getFullResult: Specimen.medtechId -> User.username.
    `AnalysisResult.medtechName` (the dead column) is deliberately seeded with
    a wrong value by `_makeResultRow`'s default — if this test passes with
    the *correct* name, the dead column definitely isn't being read.
    """
    row = _makeResultRow(resultStatus="RELEASED", patientId=PATIENT_ID)
    getMap = {
        (Specimen, SPECIMEN_ID): _makeSpecimen(medtechId=MEDTECH_ID),
        (User, MEDTECH_ID): _makeUser(username="mcruz.medtech"),
    }
    db = _makeDetailDb(PATIENT_ID, row, getMap)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert result.analyzedBy == "mcruz.medtech"


@pytest.mark.asyncio
async def test_getResultDetailAnalyzedByIsNoneWhenSpecimenHasNoMedtech():
    row = _makeResultRow(resultStatus="RELEASED", patientId=PATIENT_ID)
    getMap = {(Specimen, SPECIMEN_ID): _makeSpecimen(medtechId=None)}
    db = _makeDetailDb(PATIENT_ID, row, getMap)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert result.analyzedBy is None


@pytest.mark.asyncio
async def test_getResultDetailConfirmationNotesReflectsWrittenInterpretation():
    """Once ResultConfirmationService actually writes
    `AnalysisResult.interpretation`, this existing read path (`row.
    interpretation`) picks it up correctly on its own — no second read path.
    """
    row = _makeResultRow(
        resultStatus="RELEASED",
        patientId=PATIENT_ID,
        interpretation="No significant abnormalities detected.",
    )
    db = _makeDetailDb(PATIENT_ID, row)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert result.confirmationNotes == "No significant abnormalities detected."


@pytest.mark.asyncio
async def test_getResultDetailConfirmationNotesIsNoneWhenMedtechAddedNone():
    """A MedTech choosing not to add notes must still come through as `None`
    (the field is optional) — PdfService's "Pending review" fallback handles
    the display side of this, not a backend default string.
    """
    row = _makeResultRow(resultStatus="RELEASED", patientId=PATIENT_ID, interpretation=None)
    db = _makeDetailDb(PATIENT_ID, row)
    service = PatientResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.getResultDetail(RESULT_ID, USER_ID, _fakeRequest())

    assert result.confirmationNotes is None


@pytest.mark.asyncio
async def test_patientResultDetailResponseNeverCarriesRawSmartDiagnosisScores():
    """Raw AI probability scores (gout_score/gn_score/nephro_score/etc., as
    `SmartDiagnosisDetail` exposes to the physician/supervisor views) must
    never reach this patient-facing schema at all — not just be hidden by
    the frontend. Structural: checked against the model's own field set,
    not a single serialized instance.
    """
    fieldNames = set(PatientResultDetailResponse.model_fields)
    leakedScoreFields = {
        "goutScore", "gnScore", "nephroScore", "utiScore", "trichoScore",
        "evidenceMap", "smartDiagnosis", "engineVersion",
    }
    assert fieldNames.isdisjoint(leakedScoreFields)
    assert "smartDiagnosisUnavailable" in fieldNames
