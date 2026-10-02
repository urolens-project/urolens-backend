"""Unit tests — PhysicianResultService (SQLAlchemy AsyncSession implementation)

Covers normal list/detail behavior plus the bundled fixes in
`get_result_detail`/`list_results`:

1. Status-gate fix — same shape as PatientResultService's: a result that
   hasn't reached RELEASED must not expose full clinical detail, even to a
   physician who otherwise has access to it.
2. Ownership-scoping fix (UROLENS-153) — both methods used to scope access by
   "does this physician have *any* lab request for this result's patient",
   which let a physician see/retrieve a result tied to a *different*
   physician's lab request as long as they shared a patient. Now scoped by
   this specific result's own lab request (`Specimen.labRequestId ->
   LabRequest.physicianId`) instead — a patient with lab requests from two
   different physicians is a normal case, not an edge case.
3. "Requested On" source fix (UROLENS-153) — `PhysicianResultSummary.
   createdAt` must come from `LabRequest.createdAt` (when the physician
   submitted the request), not `AnalysisResult.createdAt` (when the AI engine
   produced the result, well downstream of it) — proven with a seeded gap
   between the two timestamps.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.models.analysis_result import AnalysisResult
from src.models.image import Image
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.models.specimen import Specimen
from src.services.physician_result_service import PhysicianResultService

PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000070")
OTHER_PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000076")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000071")
OTHER_PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000073")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000074")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000075")
LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000077")


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


def _makeSpecimen(labRequestId=LAB_REQUEST_ID):
    spec = MagicMock(spec=Specimen)
    spec.specimenId = SPECIMEN_ID
    spec.patientUid = "PAT-000071"
    spec.patientName = ""
    spec.medtechId = None
    spec.labRequestId = labRequestId
    return spec


def _makeLabRequest(physicianId=PHYSICIAN_ID, labRequestId=LAB_REQUEST_ID, createdAt=None):
    lr = MagicMock(spec=LabRequest)
    lr.labRequestId = labRequestId
    lr.physicianId = physicianId
    lr.createdAt = createdAt or datetime.now(UTC)
    return lr


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


def _makeDetailDb(getMap: dict) -> AsyncMock:
    """`db.get(Model, id)` backed by `getMap`. `db.execute()` is called twice
    in `get_result_detail`'s happy path (SmartDiagnosisOutput lookup,
    ResultReview.annotation_notes lookup) — the access-gate tests below never
    get past the ownership/status check, so neither needs a real return value
    for them.
    """
    db = AsyncMock()

    async def _get(model, id_):
        return getMap.get((model, id_))

    db.get = AsyncMock(side_effect=_get)

    sdoResult = MagicMock()
    sdoResult.scalars.return_value.all.return_value = []

    reviewResult = MagicMock()
    reviewResult.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[sdoResult, reviewResult])
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
    lr = _makeLabRequest(physicianId=PHYSICIAN_ID)
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): spec,
        (LabRequest, LAB_REQUEST_ID): lr,
        (Patient, PATIENT_ID): pat,
    }
    db = _makeDetailDb(getMap)
    auditLogger = _makeAuditLogger()
    service = PhysicianResultService(db=db, auditLogger=auditLogger)

    result = await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert result.status == "RELEASED"
    assert result.resultId == str(RESULT_ID)
    db.add.assert_called_once()
    auditLogger.record.assert_awaited_once()
    assert auditLogger.record.call_args.kwargs["eventType"] == "RESULT_RETRIEVED"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_getResultDetailGivesTheImageAsAShortLivedSignedUrlNotAPublicOne():
    # SEC-0b: the microscopy bucket is private; a public link would 400 and,
    # while it was public, exposed the image to anyone holding the link.
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    ar.imageId = uuid.uuid4()
    storageKey = f"specimens/{SPECIMEN_ID}/images/{ar.imageId}.jpg"
    signedUrl = f"https://example.supabase.co/storage/v1/object/sign/microscopy/{storageKey}?token=t"
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): _makeSpecimen(),
        (LabRequest, LAB_REQUEST_ID): _makeLabRequest(physicianId=PHYSICIAN_ID),
        (Patient, PATIENT_ID): _makePatient(),
        (Image, ar.imageId): SimpleNamespace(storageKey=storageKey),
    }
    db = _makeDetailDb(getMap)
    bucket = MagicMock()
    bucket.create_signed_url = AsyncMock(return_value={"signedURL": signedUrl, "signedUrl": signedUrl})
    fakeSb = MagicMock()
    fakeSb.storage.from_.return_value = bucket
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with patch("src.core.storage.supabase", fakeSb):
        result = await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert result.imageUrl == signedUrl
    assert "/object/public/" not in result.imageUrl
    assert bucket.create_signed_url.await_args.args[0] == storageKey


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
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): _makeSpecimen(),
        (LabRequest, LAB_REQUEST_ID): _makeLabRequest(physicianId=PHYSICIAN_ID),
    }
    db = _makeDetailDb(getMap)
    auditLogger = _makeAuditLogger()
    service = PhysicianResultService(db=db, auditLogger=auditLogger)

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "RESULT_NOT_RELEASED"
    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── getResultDetail — lab-request-level ownership scoping (UROLENS-153) ─────

@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenResultBelongsToADifferentPhysiciansLabRequest():
    """The core UROLENS-153 bug: a patient can have lab requests from more
    than one physician. A result for this patient, tied to a lab request
    *another* physician submitted, must be denied to this physician even
    though they do share a patient — the prior patient-level scoping let this
    through.
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): _makeSpecimen(),
        (LabRequest, LAB_REQUEST_ID): _makeLabRequest(physicianId=OTHER_PHYSICIAN_ID),
    }
    db = _makeDetailDb(getMap)
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenPatientNotInPhysicianScope():
    """Baseline ownership check — a result for a patient outside this
    physician's own lab-request-linked patients must still be denied.
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=OTHER_PATIENT_ID)
    getMap = {
        (AnalysisResult, RESULT_ID): ar,
        (Specimen, SPECIMEN_ID): _makeSpecimen(),
        (LabRequest, LAB_REQUEST_ID): _makeLabRequest(physicianId=OTHER_PHYSICIAN_ID),
    }
    db = _makeDetailDb(getMap)
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403


@pytest.mark.asyncio
async def test_getResultDetailRaises403WhenResultHasNoSpecimen():
    """A result with no specimen (and therefore no traceable lab request)
    can't belong to any physician's own lab requests either — denied, not
    silently allowed through.
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID, specimenId=None)
    db = _makeDetailDb({(AnalysisResult, RESULT_ID): ar})
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(HTTPException) as excInfo:
        await service.getResultDetail(RESULT_ID, PHYSICIAN_ID, _fakeRequest())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "ACCESS_DENIED"


# ── listResults — normal behavior + placeholder status ───────────────────────

@pytest.mark.asyncio
async def test_listResultsReturnsEmptyWhenNoMatchingResults():
    countResult = MagicMock()
    countResult.scalar_one.return_value = 0
    pageResult = MagicMock()
    pageResult.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[countResult, pageResult])
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    assert result.items == []
    assert result.total == 0
    assert result.page == 1
    assert result.pageSize == 20
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_listResultsShowsPlaceholderForUnreleasedRows():
    countResult = MagicMock()
    countResult.scalar_one.return_value = 2

    pendingRow = _makeAnalysisResult(resultStatus="APPROVED", patientId=PATIENT_ID)
    releasedRow = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    releasedRow.resultId = uuid.uuid4()
    pageResult = MagicMock()
    pageResult.all.return_value = [
        (pendingRow, datetime.now(UTC)),
        (releasedRow, datetime.now(UTC)),
    ]

    specResult = MagicMock()
    specResult.scalars.return_value.all.return_value = [_makeSpecimen()]

    patResult = MagicMock()
    patResult.scalars.return_value.all.return_value = [_makePatient()]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[countResult, pageResult, specResult, patResult])
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    byId = {item.resultId: item for item in result.items}
    assert byId[str(pendingRow.resultId)].status == "PENDING"
    assert byId[str(releasedRow.resultId)].status == "RELEASED"
    assert result.total == 2


@pytest.mark.asyncio
async def test_listResultsRequestedOnReflectsLabRequestNotAnalysisResult():
    """UROLENS-153: `createdAt` on each item is "Requested On" — must come
    from `LabRequest.createdAt`, not `AnalysisResult.createdAt`. Seeded with a
    real gap between the two (the lab request predates the AI-produced
    result by 3 days) so a bug sourcing the wrong column fails loudly instead
    of coincidentally matching.
    """
    ar = _makeAnalysisResult(resultStatus="RELEASED", patientId=PATIENT_ID)
    labRequestCreatedAt = ar.createdAt - timedelta(days=3)

    countResult = MagicMock()
    countResult.scalar_one.return_value = 1
    pageResult = MagicMock()
    pageResult.all.return_value = [(ar, labRequestCreatedAt)]
    specResult = MagicMock()
    specResult.scalars.return_value.all.return_value = [_makeSpecimen()]
    patResult = MagicMock()
    patResult.scalars.return_value.all.return_value = [_makePatient()]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[countResult, pageResult, specResult, patResult])
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    result = await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    assert result.items[0].createdAt == labRequestCreatedAt.isoformat()
    assert result.items[0].createdAt != ar.createdAt.isoformat()


@pytest.mark.asyncio
async def test_listResultsCountAndPageQueriesScopeOnLabRequestPhysicianId():
    """UROLENS-153: scoping must be `LabRequest.physician_id == physician_id`
    on the result's own lab request — not a separate "which patients does
    this physician have any lab request for" pre-query. Checked structurally
    on the compiled SQL (both the count and page statements), and confirms
    only two round trips happen for an empty result page — no N+1 fan-out
    before the scoping filter is even applied.
    """
    countResult = MagicMock()
    countResult.scalar_one.return_value = 0
    pageResult = MagicMock()
    pageResult.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[countResult, pageResult])
    service = PhysicianResultService(db=db, auditLogger=_makeAuditLogger())

    await service.listResults(PHYSICIAN_ID, page=1, pageSize=20)

    assert db.execute.await_count == 2
    for stmt in (db.execute.await_args_list[0].args[0], db.execute.await_args_list[1].args[0]):
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "lab_requests.physician_id" in compiled
        assert PHYSICIAN_ID.hex in compiled.replace("-", "")
