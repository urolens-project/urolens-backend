"""Unit tests — multi-field web lane, phase W0 (unconditional fixes).

1. Corrected counts reach the patient portal and the PDF (`core.particle_counts`,
   `PatientResultService.getResultDetail`).
2. Supervisor approve / return / escalate write an audit row in their own
   transaction, with ids only.
3. The override limit is 1,000.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from src.api.results import getResultReviewService
from src.core.audit_logger import AuditLogger, getAuditLogger
from src.core.database import getDb
from src.core.exceptions import ConflictException
from src.core.particle_counts import settledCounts, toWholeCount
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.specimen import Specimen
from src.schemas.patient_portal import (
    PARTICLE_LABELS,
    PatientResultDetail,
)
from src.schemas.result_review import MAX_OVERRIDE_COUNT, OverrideRequest
from src.services import pdf_service
from src.services.patient_result_service import PatientResultService
from src.services.result_review_service import ResultReviewService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a2")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a3")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a4")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a5")


# ── particle_counts helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4, 4), (0, 0), (7.0, 7), ("7.0", 7), ("12", 12), (3.8, 4), (3.4, 3),
        (2.5, 3), (0.5, 1), (1.5, 2), (None, 0), ("many", 0), ("", 0), (-3, 0),
        (float("nan"), 0), (float("inf"), 0), (True, 0),
    ],
)
def test_toWholeCountRoundsHalfUpAndToleratesBadValues(value, expected):
    assert toWholeCount(value) == expected


def test_settledCountsOverlaysParticleClassesOnAiFindings():
    assert settledCounts({"erythrocytes": 5, "bacteria": 2}, {"erythrocytes": 3}) == {
        "erythrocytes": 3, "bacteria": 2,
    }


@pytest.mark.parametrize("classes", [None, [], ["erythrocytes"], "x", 5])
def test_settledCountsIgnoresAParticleClassesThatIsNotADict(classes):
    assert settledCounts({"bacteria": 2}, classes) == {"bacteria": 2}


@pytest.mark.parametrize("findings", [None, [], "x"])
def test_settledCountsToleratesMissingFindings(findings):
    assert settledCounts(findings, {"bacteria": 2}) == {"bacteria": 2}


# ── patient detail and PDF ───────────────────────────────────────────────────

def _row(aiFindings, particleClasses):
    row = MagicMock(spec=AnalysisResult)
    row.resultId = RESULT_ID
    row.status = "RELEASED"
    row.patientId = PATIENT_ID
    row.specimenId = SPECIMEN_ID
    row.releasedAt = None
    row.confirmedAt = None
    row.interpretation = None
    row.medtechName = None
    row.aiFindings = aiFindings
    row.particleClasses = particleClasses
    row.smartDiagnosisUnavailable = False
    return row


async def _patientDetail(row) -> PatientResultDetail:
    spec = MagicMock(spec=Specimen)
    spec.specimenId = SPECIMEN_ID
    spec.medtechId = None
    resolved = MagicMock()
    resolved.scalar_one_or_none.return_value = PATIENT_ID
    db = AsyncMock()
    db.execute = AsyncMock(return_value=resolved)

    async def _get(model, entityId):
        return row if model is AnalysisResult else spec

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()
    db.commit = AsyncMock()
    auditLogger = MagicMock(record=AsyncMock())
    request = MagicMock()
    request.client.host = "127.0.0.1"
    return await PatientResultService(db=db, auditLogger=auditLogger).getResultDetail(
        RESULT_ID, USER_ID, request
    )


def _count(detail: PatientResultDetail, label: str) -> int:
    return next(p.count for p in detail.particleCounts if p.label == label)


@pytest.mark.asyncio
async def test_aMedtechCorrectionReachesThePatient():
    # AI found 9 erythrocytes; the MedTech corrected it to 6 (float, as confirm stores it).
    detail = await _patientDetail(
        _row({"erythrocytes": 9, "bacteria": 2}, {"erythrocytes": 6.0, "bacteria": 2})
    )
    assert _count(detail, "erythrocytes") == 6
    assert _count(detail, "bacteria") == 2
    assert [p.label for p in detail.particleCounts] == PARTICLE_LABELS


@pytest.mark.asyncio
async def test_withNoCorrectionsThePatientSeesTheAiCounts():
    detail = await _patientDetail(_row({"bacteria": 5}, {}))
    assert _count(detail, "bacteria") == 5
    assert _count(detail, "crystals") == 0


@pytest.mark.asyncio
async def test_aDecimalSettledCountIsRoundedHalfUpNotTruncated():
    detail = await _patientDetail(_row({"leukocytes": 3.8}, {}))
    assert _count(detail, "leukocytes") == 4


@pytest.mark.asyncio
async def test_aCorrectionReachesThePdf():
    detail = await _patientDetail(_row({"erythrocytes": 9}, {"erythrocytes": 6}))
    captured: list = []

    def _fakeTable(data, *args, **kwargs):
        captured.extend(data)
        return MagicMock()

    with patch("reportlab.platypus.Table", side_effect=_fakeTable), \
         patch("reportlab.platypus.SimpleDocTemplate.build", return_value=None):
        pdf_service.generateResultPdf(detail, "Jane Doe", uuid.uuid4())

    rbcRow = next(row for row in captured if "Red Blood Cells (RBC)" in str(row))
    assert "6" in str(rbcRow)
    assert "9" not in str(rbcRow)


# ── supervisor audit rows ────────────────────────────────────────────────────

def _result():
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL
    return result


def _specimen():
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.medtechId = None
    specimen.sampleUid = "SMP-1"
    specimen.completedAt = None
    return specimen


def _db(getSideEffect, rowcount=1):
    db = AsyncMock()
    db.get = AsyncMock(side_effect=getSideEffect)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    executed = MagicMock()
    executed.rowcount = rowcount
    db.execute = AsyncMock(return_value=executed)
    return db


def _service(db):
    auditLogger = MagicMock(record=AsyncMock())
    notif = MagicMock(notifyMedtechResultReturned=AsyncMock())
    return ResultReviewService(db=db, auditLogger=auditLogger, _notifService=notif), auditLogger


def _assertOneAuditRow(auditLogger, eventType, db, request, extraKeys=()):
    auditLogger.record.assert_awaited_once()
    kwargs = auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == eventType
    assert kwargs["entityType"] == "analysis_result"
    assert kwargs["entityId"] == RESULT_ID
    assert kwargs["userId"] == SUPERVISOR_ID
    assert kwargs["db"] is db
    assert kwargs["request"] is request
    assert set(kwargs["detailJson"]) == {"specimen_id", *extraKeys}
    assert kwargs["detailJson"]["specimen_id"] == str(SPECIMEN_ID)


@pytest.mark.asyncio
async def test_approveWritesOneAuditRowBeforeTheCommit():
    db = _db([_result(), _specimen()])
    service, auditLogger = _service(db)
    request = MagicMock()
    order: list[str] = []
    auditLogger.record.side_effect = lambda **_: order.append("audit")
    db.commit.side_effect = lambda: order.append("commit")

    await service.approveResult(RESULT_ID, SUPERVISOR_ID, notes="Secret approval note", request=request)

    _assertOneAuditRow(auditLogger, "RESULT_APPROVED", db, request)
    assert order == ["audit", "commit"]
    assert "Secret approval note" not in str(auditLogger.record.call_args)


@pytest.mark.asyncio
async def test_returnWritesOneAuditRowWithoutTheReason():
    specimen = _specimen()
    db = _db([_result(), specimen])
    service, auditLogger = _service(db)
    request = MagicMock()

    await service.returnResult(RESULT_ID, SUPERVISOR_ID, reason="Secret return reason", request=request)

    _assertOneAuditRow(auditLogger, "RESULT_RETURNED", db, request)
    assert "Secret return reason" not in str(auditLogger.record.call_args)


@pytest.mark.asyncio
async def test_escalateWritesOneAuditRowWithThePathButNotTheNote():
    db = _db([_result()])
    service, auditLogger = _service(db)
    request = MagicMock()

    await service.escalateResult(
        RESULT_ID, SUPERVISOR_ID, escalationPath="MARK_CRITICAL",
        escalationNote="Secret escalation note", request=request,
    )

    _assertOneAuditRow(auditLogger, "RESULT_ESCALATED", db, request, extraKeys=("escalation_path",))
    assert auditLogger.record.call_args.kwargs["detailJson"]["escalation_path"] == "MARK_CRITICAL"
    assert "Secret escalation note" not in str(auditLogger.record.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "return", "escalate"])
async def test_aLostRaceLeavesNoAuditRow(action):
    getSideEffect = [_result(), _specimen()] if action != "escalate" else [_result()]
    db = _db(getSideEffect, rowcount=0)
    service, auditLogger = _service(db)

    with pytest.raises(ConflictException):
        if action == "approve":
            await service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)
        elif action == "return":
            await service.returnResult(RESULT_ID, SUPERVISOR_ID, reason="r")
        else:
            await service.escalateResult(RESULT_ID, SUPERVISOR_ID, "MARK_CRITICAL", None)

    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aResultNotAwaitingApprovalLeavesNoAuditRow():
    result = _result()
    result.status = ResultStatus.APPROVED
    db = _db([result])
    service, auditLogger = _service(db)

    with pytest.raises(ConflictException):
        await service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)

    auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_aServiceBuiltWithoutAnAuditLoggerStillWorks():
    db = _db([_result()])
    response = await ResultReviewService(db=db).escalateResult(
        RESULT_ID, SUPERVISOR_ID, "MARK_CRITICAL", None
    )
    assert response["status"] == ResultStatus.CRITICAL_ESCALATED.value


# ── routes pass the request through ──────────────────────────────────────────

@pytest.fixture
def routeClient():
    seen: dict = {}
    fake = SimpleNamespace(
        approveResult=AsyncMock(side_effect=lambda **kw: seen.update(approve=kw) or {
            "resultId": RESULT_ID, "status": "APPROVED", "approvedAt": "2026-10-27T00:00:00+08:00"}),
        returnResult=AsyncMock(side_effect=lambda **kw: seen.update(ret=kw) or {
            "resultId": RESULT_ID, "status": "RETURNED_FOR_CORRECTION",
            "returnedAt": "2026-10-27T00:00:00+08:00"}),
        escalateResult=AsyncMock(side_effect=lambda **kw: seen.update(escalate=kw) or {
            "resultId": RESULT_ID, "status": "CRITICAL_ESCALATED", "escalationPath": "MARK_CRITICAL",
            "escalatedAt": "2026-10-27T00:00:00+08:00"}),
    )
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(SUPERVISOR_ID), "role": "SUPERVISOR"}
    app.dependency_overrides[getResultReviewService] = lambda: fake
    yield TestClient(app), seen
    app.dependency_overrides.pop(getCurrentUser, None)
    app.dependency_overrides.pop(getResultReviewService, None)


def test_theThreeRoutesPassTheRequestToTheService(routeClient):
    client, seen = routeClient
    base = f"/api/v1/results/{RESULT_ID}"

    assert client.post(f"{base}/approve", json={"notes": "ok"}).status_code == 200
    assert client.post(f"{base}/return", json={"reason": "blurry"}).status_code == 200
    assert client.post(
        f"{base}/escalate", json={"escalationPath": "MARK_CRITICAL", "escalationNote": "n"}
    ).status_code == 200

    for key in ("approve", "ret", "escalate"):
        assert isinstance(seen[key]["request"], Request), key


# ── override limit ───────────────────────────────────────────────────────────

def _body(**overrides):
    return {"parameter": "erythrocytes", "correctedValue": 4, "rationale": "Recounted", **overrides}


def test_theOverrideLimitIsOneThousand():
    assert MAX_OVERRIDE_COUNT == 1000
    assert OverrideRequest(**_body(correctedValue=1000)).correctedValue == 1000
    assert OverrideRequest(**_body(correctedValue=301)).correctedValue == 301
    with pytest.raises(ValidationError):
        OverrideRequest(**_body(correctedValue=1001))


# ── production wiring: the review service always has an audit logger ─────────

_NOW = "2026-10-27T00:00:00+08:00"
_WIRING_CASES = [
    ("approveResult", "approve", {"notes": "ok"},
     {"resultId": RESULT_ID, "status": "APPROVED", "approvedAt": _NOW}),
    ("returnResult", "return", {"reason": "blurry"},
     {"resultId": RESULT_ID, "status": "RETURNED_FOR_CORRECTION", "returnedAt": _NOW}),
    ("escalateResult", "escalate", {"escalationPath": "MARK_CRITICAL"},
     {"resultId": RESULT_ID, "status": "CRITICAL_ESCALATED",
      "escalationPath": "MARK_CRITICAL", "escalatedAt": _NOW}),
]


async def _fakeDbDependency():
    yield AsyncMock()


def _callRouteWithProductionWiring(methodName, action, body, response):
    """Call a supervisor route with the real `getResultReviewService` chain (only
    the DB session and the caller are replaced) and return the service
    instance the route built."""
    built: list[ResultReviewService] = []

    def _spy(self, *args, **kwargs):
        built.append(self)
        return response

    app.dependency_overrides[getCurrentUser] = lambda: {
        "user_id": str(SUPERVISOR_ID), "role": "SUPERVISOR"}
    app.dependency_overrides[getDb] = _fakeDbDependency
    try:
        with patch.object(ResultReviewService, methodName, autospec=True, side_effect=_spy):
            reply = TestClient(app).post(f"/api/v1/results/{RESULT_ID}/{action}", json=body)
    finally:
        app.dependency_overrides.pop(getCurrentUser, None)
        app.dependency_overrides.pop(getDb, None)
        app.dependency_overrides.pop(getAuditLogger, None)
    assert reply.status_code == 200, reply.text
    assert len(built) == 1
    return built[0]


@pytest.mark.parametrize(("methodName", "action", "body", "response"), _WIRING_CASES)
def test_theProductionDependencyGivesTheReviewServiceAnAuditLogger(
    methodName, action, body, response
):
    service = _callRouteWithProductionWiring(methodName, action, body, response)

    assert service.auditLogger is not None
    assert isinstance(service.auditLogger, AuditLogger)


@pytest.mark.asyncio
async def test_theDependencyFactoryPassesTheLoggerItIsGiven():
    auditLogger = MagicMock(spec=AuditLogger)

    service = await getResultReviewService(
        db=AsyncMock(), auditLogger=auditLogger, _notifService=MagicMock()
    )

    assert service.auditLogger is auditLogger


def test_theWiringCheckFailsIfTheLoggerIsDropped():
    # Proves the check above is sensitive: with the logger dependency replaced by
    # None (what a dropped wiring would produce) the built service has none.
    app.dependency_overrides[getAuditLogger] = lambda: None
    methodName, action, body, response = _WIRING_CASES[0]

    service = _callRouteWithProductionWiring(methodName, action, body, response)

    assert service.auditLogger is None
