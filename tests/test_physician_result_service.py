"""Unit tests — PhysicianResultService (SQLAlchemy AsyncSession implementation)

No tests previously existed for this service (confirmed via full-repo grep
as part of the Supabase-REST → SQLAlchemy migration plan). Covers normal
list/detail behavior plus two distinct, separately-tested bundled fixes in
`get_result_detail`:

1. Status-gate fix — same shape as PatientResultService's: a result that
   hasn't reached RELEASED must not expose full clinical detail, even to a
   physician who otherwise has access to it.
2. Ownership-check gap fix — the prior `if patient_id:` guard skipped the
   *entire* ownership check when a result's patient_id was falsy, letting
   any physician retrieve any patient-less result with no check at all.
   This is a different condition than the status gate (it's about *whose*
   result it is, not *what state* it's in) even though both live in the
   same method, so it gets its own test rather than being folded into the
   status-gate test.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.models.analysis_result import AnalysisResult
from src.models.patient import Patient
from src.models.specimen import Specimen
from src.services.physician_result_service import PhysicianResultService

PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000070")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000071")
OTHER_PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000073")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000074")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000075")


# ── Row builders ─────────────────────────────────────────────────────────────

def _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID, specimenId=SPECIMEN_ID):
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = RESULT_ID
    ar.status = resultStatus
    ar.patientId = patientId
    ar.specimenId = specimenId
    ar.imageId = None
    ar.confirmedAt = None
    ar.createdAt = datetime.now(UTC)
    ar.aiFindings = {}
    ar.flaggedAnomalies = {}
    ar.particleClasses = {}
    ar.modelVersion = "mvp-v1.0"
    ar.smartDiagnosisUnavailable = True
    return ar


def _makeSpecimen():
    spec = MagicMock(spec=Specimen)
    spec.specimenId = SPECIMEN_ID
    spec.patientUid = "PAT-000071"
    spec.patientName = ""
    spec.medtechId = None
    return spec


def _makePatient():
    pat = MagicMock(spec=Patient)
    pat.patientId = PATIENT_ID
    pat.patientUid = "PAT-000071"
    pat.firstName = None
    pat.lastName = None
    pat.dateOfBirth = None
    pat.sex = "MALE"
    return pat


def _makeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


def _fakeRequest() -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _makeDetailDb(getMap: dict, physicianPatientIds: set) -> AsyncMock:
    """`db.get(Model, id)` backed by `getMap`. `db.execute()` is called three
    times in `get_result_detail`'s happy path (physician-patient-id scoping,
    SmartDiagnosisOutput lookup, ResultReview.annotation_notes lookup) — the
    two DB-access-gate tests below never get past the first, so only that one
    needs a real return value for them.
    """
    db = AsyncMock()

    async def _get(model, id_):
        return getMap.get((model, id_))

    db.get = AsyncMock(side_effect=_get)

    physicianIdsResult = MagicMock()
    physicianIdsResult.scalars.return_value.all.return_value = list(physicianPatientIds)

    sdoResult = MagicMock()
    sdoResult.scalars.return_value.all.return_value = []

    reviewResult = MagicMock()
    reviewResult.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[physicianIdsResult, sdoResult, reviewResult])
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


# ── getResultDetail — normal behavior ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_getResultDetailRaises404WhenResultMissing():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 404


@pytest.mark.asyncio
async def test_getResultDetailReturnsFullDetailForOwnedReleasedResult():
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    spec = _makeSpecimen()
    pat = _makePatient()
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): spec,
        (Patient, PATIENT_ID): pat,
    }
    db = _makeDetailDb(getMap, physicianPatientIds={PATIENT_ID})
    auditLogger = _makeAuditLogger()
    service = PhysicianResultService(db=db, auditLogger=auditLogger)

    result = await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert result.status == "RELEASED"
    assert result.resultId == str(RESULT_ID)
    db.add.assert_called_once()
    auditLogger.record.assert_awaited_once()
    assert auditLogger.record.call_args.kwargs["eventType"] == "RESULT_RETRIEVED"
    db.commit.assert_awaited_once()


# ── getResultDetail — status-gate fix (security-relevant) ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "badStatus",
    [
        "PENDING_CONFIRM",
        "PENDING_SUPERVISOR_APPROVAL",
        "APPROVED",
        "CRITICAL_ESCALATED",
    ],
)
async def test_getResultDetailRaises403ForOwnedButNotYetReleasedResult(badStatus):
    """A physician must not see full clinical detail for a result they have
    legitimate access to (their own patient) before it's RELEASED.
    """
    ar = _makeAnalysisResult(resultStatus=badStatus, patientId=PATIENT_ID)
    db = _makeDetailDb({(AnalysisResult, RESULT_ID): ar}, physicianPatientIds={PATIENT_ID})
    auditLogger = _makeAuditLogger()
    service = PhysicianResultService(db=db, auditLogger=auditLogger)

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "RESULT_NOT_RELEASED"
    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── getResultDetail — ownership-check gap fix (security-relevant, distinct) ──

@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenPatientNotInPhysicianScope():
    """Baseline ownership check — a result for a patient outside this
    physician's own lab-request-linked patients must still be denied
    (unchanged behavior, kept as a control for the falsy-patient_id test below).
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=OTHER_PATIENT_ID)
    db = _makeDetailDb({(AnalysisResult, RESULT_ID): ar}, physicianPatientIds={PATIENT_ID})
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403


@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenPatientIdIsFalsy():
    """The bug: `if patient_id:` used to guard the *entire* ownership check,
    so a result with no patient_id at all skipped the check completely and
    was returned to any physician. Now the check always runs, and a falsy
    patient_id is treated as "not this physician's patient" — denied, not
    silently allowed through.
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=None)
    db = _makeDetailDb({(AnalysisResult, RESULT_ID): ar}, physicianPatientIds={PATIENT_ID})
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403


# ── listResults — normal behavior + placeholder status ───────────────────────

@pytest.mark.asyncio
async def test_listResultsReturnsEmptyWhenPhysicianHasNoPatients():
    db = AsyncMock()
    noPatientIds = MagicMock()
    noPatientIds.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=noPatientIds)
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    assert result.items == []
    assert result.total == 0
    assert result.page == 1
    assert result.pageSize == 20


@pytest.mark.asyncio
async def test_listResultsShowsPlaceholderForUnreleasedRows():
    physicianIdsResult = MagicMock()
    physicianIdsResult.scalars.return_value.all.return_value = [PATIENT_ID]

    countResult = MagicMock()
    countResult.scalar_one.return_value = 2

    pendingRow = _makeAnalysisResult(resultStatus="APPROVED", patientId=PATIENT_ID)
    releasedRow = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    releasedRow.resultId = uuid.uuid4()
    pageResult = MagicMock()
    pageResult.scalars.return_value.all.return_value = [pendingRow, releasedRow]

    specResult = MagicMock()
    specResult.scalars.return_value.all.return_value = [_makeSpecimen()]

    patResult = MagicMock()
    patResult.scalars.return_value.all.return_value = [_makePatient()]

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[physicianIdsResult, countResult, pageResult, specResult, patResult]
    )
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    byId = {item.resultId: item for item in result.items}
    assert byId[str(pendingRow.resultId)].status == "PENDING"
    assert byId[str(releasedRow.resultId)].status == "RELEASED"
    assert result.total == 2
