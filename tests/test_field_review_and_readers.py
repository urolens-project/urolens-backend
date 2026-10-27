"""Unit tests — multi-field web lane, phase W4: review, return, retirements, readers.

Flag-off responses are checked against golden JSON captured from the code before W4
(`tests/fixtures/*_golden.json`): the flag off must not change a single byte.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from src.api.patient_portal import getPatientResultService
from src.api.physician import getPhysicianResultService
from src.api.results import (
    getConfirmationService,
    getOverrideService,
    getResultReviewService,
)
from src.core.config import settings
from src.core.rbac import getCurrentUser
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import Image
from src.models.manual_override import ManualOverride
from src.models.patient import Patient
from src.models.result_field import FieldStatus
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.field import FieldSummaryItem
from src.schemas.patient_portal import (
    ParticleCount,
    PatientResultDetail,
    PatientResultDetailResponse,
)
from src.schemas.physician import PhysicianResultDetail
from src.schemas.result_review import (
    FullResultDetail,
    MedtechPendingListResponse,
    ReturnResponse,
)
from src.services import pdf_service
from src.services.field_aggregation_service import (
    AcceptedField,
    aggregate,
    storedFieldSummary,
)
from src.services.patient_result_service import PatientResultService
from src.services.physician_result_service import PhysicianResultService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_review_service import ResultReviewService

FIXTURES = Path(__file__).parent / "fixtures"
RID = uuid.UUID("00000000-0000-0000-0000-0000000014a1")
SID = uuid.UUID("00000000-0000-0000-0000-0000000014a2")
UID = uuid.UUID("00000000-0000-0000-0000-0000000014a3")
OTHER_RESULT = uuid.UUID("00000000-0000-0000-0000-0000000014a9")
FIELD_ID = uuid.UUID("00000000-0000-0000-0000-0000000015a1")
AT = datetime(2026, 10, 27, 1, 2, 3, tzinfo=UTC)

AI_RBC = (3, 5, 2, 9, 4, 4, 1, 6, 3, 4)
EFFECTIVE_RBC = (3, 5, 2, 6, 4, 4, 1, 6, 3, 4)


def storedSummary(extra: dict | None = None) -> dict:
    """The stored field_summary for the proposal example, plus casts seen in 3 of 10 fields."""
    fields = []
    for n, (ai, effective) in enumerate(zip(AI_RBC, EFFECTIVE_RBC, strict=True), start=1):
        casts = 1 if n <= 3 else 0
        fields.append(AcceptedField(
            fieldNo=n, aiFindings={"erythrocytes": ai, "urinary_casts": casts},
            effectiveCounts={"erythrocytes": effective, "urinary_casts": casts}))
    return storedFieldSummary(aggregate(fields, extra or {}))


@pytest.fixture(autouse=True)
def cleanOverrides():
    yield
    app.dependency_overrides.clear()


def login(role: str) -> None:
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(UID), "role": role}


def golden(name: str) -> str:
    return (FIXTURES / f"{name}_golden.json").read_text(encoding="utf-8").rstrip("\n")


# ── flag off: byte-identical to the golden JSON ──────────────────────────────

def _fullDetail():
    return {
        "resultId": RID, "specimenId": SID, "sampleUid": "SMP-20261027-00001", "patientUid": "PAT-0001",
        "patientName": None, "patientAge": 41, "patientSex": "F", "medtechName": "jdoe", "confirmedAt": AT,
        "confirmationNotes": None, "aiFindings": {"erythrocytes": 9}, "aiDetections": [],
        "flaggedAnomalies": {"erythrocytes": "above"}, "particleClasses": {"erythrocytes": 6},
        "modelVersion": "mvp-v1.0",
        "manualOverrides": [{
            "overrideId": uuid.UUID("00000000-0000-0000-0000-0000000014b1"), "parameterName": "erythrocytes",
            "originalAiValue": "9.0", "correctedValue": "6.0", "rationale": "Recounted", "overriddenAt": AT,
            "overriddenBy": UID, "overriddenByName": "jdoe"}],
        "imageUrl": "https://signed.example/a", "smartDiagnosis": None, "smartDiagnosisUnavailable": False,
        "status": "RETURNED_FOR_CORRECTION", "returnReason": "Blurry image", "isRetakePending": False,
        "annotations": [],
    }


def _patientDetail():
    return PatientResultDetailResponse(
        status="RELEASED", confirmedAt=AT, confirmationNotes="Normal", analyzedBy="jdoe",
        requestingPhysician="Dr. Reyes",
        particleCounts=[ParticleCount(label="erythrocytes", count=6), ParticleCount(label="bacteria", count=0)],
        particleClasses=["erythrocytes"], smartDiagnosisUnavailable=False, testType="Urinalysis", releasedAt=AT)


def _physicianDetail():
    return PhysicianResultDetail(
        resultId=str(RID), specimenId=str(SID), patientName="Jane Doe", patientUid="PAT-0001", patientAge=41,
        patientSex="F", medtechName="jdoe", confirmedAt=AT.isoformat(), aiFindings={"erythrocytes": 9},
        flaggedAnomalies={"erythrocytes": "above"}, particleClasses={"erythrocytes": 6}, modelVersion="mvp-v1.0",
        smartDiagnosis=None, imageUrl=None, status="APPROVED", annotationNotes=None)


def _physicianPath() -> str:
    path = next(r.path for r in app.routes if getattr(r, "path", "").endswith("/results/{result_id}")
                and "physician" in r.path)
    return path.replace("{result_id}", str(RID))


@pytest.mark.parametrize("flag", [False, True])
def test_theFullResultJsonIsTheGoldenWhenNothingIsFieldBased(monkeypatch, flag):
    # Flag off, and also flag on when the service adds no multi-field keys.
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", flag)
    app.dependency_overrides[getResultReviewService] = lambda: MagicMock(
        getFullResult=AsyncMock(return_value=_fullDetail()))
    login("SUPERVISOR")
    response = TestClient(app).get(f"/api/v1/results/{RID}")
    assert response.status_code == 200 and response.text == golden("full_result")


def test_theReturnResponseJsonIsTheGoldenWhenTheServiceAddsNoPointer(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    review = MagicMock(returnResult=AsyncMock(
        return_value={"resultId": RID, "status": "RETURNED_FOR_CORRECTION", "returnedAt": AT}))
    app.dependency_overrides[getResultReviewService] = lambda: review
    login("SUPERVISOR")
    response = TestClient(app).post(f"/api/v1/results/{RID}/return", json={"reason": "Blurry image"})
    assert response.status_code == 200 and response.text == golden("return_response")


def test_thePatientDetailJsonIsTheGolden():
    app.dependency_overrides[getPatientResultService] = lambda: MagicMock(
        getResultDetail=AsyncMock(return_value=_patientDetail()))
    login("PATIENT")
    response = TestClient(app).get(f"/api/v1/patient/results/{RID}")
    assert response.status_code == 200 and response.text == golden("patient_detail")


def test_thePhysicianDetailJsonIsTheGolden():
    app.dependency_overrides[getPhysicianResultService] = lambda: MagicMock(
        getResultDetail=AsyncMock(return_value=_physicianDetail()))
    login("PHYSICIAN")
    response = TestClient(app).get(_physicianPath())
    assert response.status_code == 200 and response.text == golden("physician_detail")


def test_theNewKeysAreDescribedInOpenapiEvenThoughOmittedFromTheJson():
    schemas = TestClient(app).get("/openapi.json").json()["components"]["schemas"]
    assert {"fields", "fieldCount", "fieldSummary", "isLegacySingleField", "returnFieldId",
            "returnParticleType"} <= set(schemas["FullResultDetail"]["properties"])
    assert {"fieldId", "particleType"} <= set(schemas["ReturnResponse"]["properties"])
    assert {"fieldId", "particleType"} <= set(schemas["ReturnRequest"]["properties"])
    assert {"returnFieldId", "returnParticleType"} <= set(schemas["MedtechPendingResultItem"]["properties"])
    assert {"fieldCount", "countsLabel"} <= set(schemas["PatientResultDetailResponse"]["properties"])
    assert "displayValue" in schemas["ParticleCount"]["properties"]
    assert {"fieldCount", "countsLabel", "fieldSummary"} <= set(schemas["PhysicianResultDetail"]["properties"])


# ── GET /results/{id}: the full result with fields ───────────────────────────

class ReviewDb:
    """Fake session for `getFullResult`: routes `execute` by compiled SQL."""

    def __init__(self, ar, overrides=(), returnRow=None, flag=True):
        self.ar = ar
        self.overrides = list(overrides)
        self.returnRow = returnRow
        self.flag = flag
        self.statements: list[str] = []

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        if "from analysis_results" in low:
            out.unique.return_value.scalar_one_or_none.return_value = self.ar
        elif "from patients" in low:
            out.scalar_one_or_none.return_value = None
        elif "from manual_overrides" in low:
            rows = [o for o in self.overrides if o.fieldId is None] if "field_id is null" in low else self.overrides
            out.scalars.return_value.all.return_value = rows
        elif "from result_reviews" in low:
            out.scalars.return_value.all.return_value = []
        elif "from smart_diagnosis_outputs" in low:
            out.scalar_one_or_none.return_value = None
        elif "from result_returns" in low:
            row = self.returnRow
            out.first.return_value = row
            out.scalar_one_or_none.return_value = row[0] if row else None
        else:  # pragma: no cover
            raise AssertionError(f"unexpected statement: {low}")
        return out


def mkAr(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL, summary="stored", legacy=False, image=True):
    ar = AnalysisResult(
        resultId=RID, specimenId=SID, status=status, aiFindings={"erythrocytes": 3.6},
        flaggedAnomalies={"erythrocytes": "above"}, particleClasses={"erythrocytes": 4}, modelVersion="mvp-v1.0",
        smartDiagnosisUnavailable=False, fieldCount=10, isLegacySingleField=legacy,
        fieldSummary=storedSummary() if summary == "stored" else summary, confirmedAt=AT)
    ar.specimen = Specimen(specimenId=SID, medtechId=UID, patientUid="PAT-0001", sampleUid="SMP-1")
    ar.specimen.medtech = User(userId=UID, username="jdoe", role="MEDTECH")
    ar.image = Image(imageId=uuid.uuid4(), storageKey="specimens/s/images/single.jpg", status="ACTIVE") if image else None
    return ar


def fieldItems(statuses=(FieldStatus.ACCEPTED, FieldStatus.ACCEPTED, FieldStatus.DISCARDED)):
    return [
        FieldSummaryItem(
            fieldId=uuid.UUID(f"00000000-0000-0000-0000-0000000016{n:02d}"), resultId=RID, fieldNo=n,
            magnification="HPF", status=status, imageUrl=f"https://signed.example/field-{n}",
            aiFindings={}, effectiveCounts={}, confidenceScores={}, version=1, createdAt=AT, updatedAt=AT)
        for n, status in enumerate(statuses, start=1)
    ]


def reviewService(db):
    return ResultReviewService(db=db, auditLogger=None, _notifService=MagicMock())


def resultLevel(parameter="erythrocytes", fieldId=None):
    row = ManualOverride(
        overrideId=uuid.uuid4(), resultId=RID, fieldId=fieldId, medtechId=UID, parameterName=parameter,
        originalAiValue="9.0", correctedValue="6.0", rationale="r", kind="OVERRIDE", overriddenAt=AT)
    row.medtech = User(userId=UID, username="jdoe", role="MEDTECH")
    return row


@pytest.fixture
def signing():
    single = AsyncMock(return_value="https://signed.example/single")
    with patch("src.services.result_review_service.signedImageUrl", single):
        yield single


@pytest.mark.asyncio
async def test_withTheFlagOnTheFullResultCarriesTheFieldsAndTheMappedSummary(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    items = fieldItems()
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=items)):
        detail = await reviewService(ReviewDb(mkAr())).getFullResult(RID, viewerRole="SUPERVISOR")

    assert detail["fields"] == items
    assert detail["fieldCount"] == 10 and detail["isLegacySingleField"] is False
    assert detail["fieldSummary"]["fieldCount"] == 10
    assert detail["fieldSummary"]["perClass"]["erythrocytes"]["displayValue"] == "4"
    assert detail["fieldSummary"]["perClass"]["urinary_casts"]["displayValue"] == "<1"
    assert "detections" not in str(detail["fields"])
    # and it validates as the response model
    assert FullResultDetail(**detail).fieldSummary.perClass["urinary_casts"].mean == 0.3


@pytest.mark.asyncio
async def test_theImageIsTheFirstAcceptedFieldsNotTheSingleImage(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    items = fieldItems((FieldStatus.DISCARDED, FieldStatus.ACCEPTED, FieldStatus.ACCEPTED))
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=items)):
        detail = await reviewService(ReviewDb(mkAr())).getFullResult(RID, viewerRole="SUPERVISOR")
    assert detail["imageUrl"] == "https://signed.example/field-2"
    signing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [(), (FieldStatus.DISCARDED, FieldStatus.ANALYZED)])
async def test_withNoAcceptedFieldTheImageIsTheSingleImageAsBefore(monkeypatch, signing, statuses):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    with patch("src.services.result_review_service.buildFieldSummaries",
               AsyncMock(return_value=fieldItems(statuses))):
        detail = await reviewService(ReviewDb(mkAr(summary=None))).getFullResult(RID, viewerRole="SUPERVISOR")
    assert detail["imageUrl"] == "https://signed.example/single"
    assert detail["fieldSummary"] is None and detail["fieldCount"] == 10


@pytest.mark.asyncio
async def test_theFlagsReplaceTheAnomaliesWhenASummaryIsPresent(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        withSummary = await reviewService(ReviewDb(mkAr())).getFullResult(RID, viewerRole="SUPERVISOR")
        without = await reviewService(ReviewDb(mkAr(summary=None))).getFullResult(RID, viewerRole="SUPERVISOR")
    assert withSummary["flaggedAnomalies"] == {}
    assert without["flaggedAnomalies"] == {"erythrocytes": "above"}


@pytest.mark.asyncio
async def test_aLegacyResultIsMarkedAndKeepsItsOldFindings(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=fieldItems((FieldStatus.ACCEPTED,)))):
        detail = await reviewService(ReviewDb(mkAr(summary=None, legacy=True))).getFullResult(RID, viewerRole="SUPERVISOR")
    assert detail["isLegacySingleField"] is True and detail["fieldSummary"] is None
    assert detail["flaggedAnomalies"] == {"erythrocytes": "above"}


@pytest.mark.asyncio
async def test_withTheFlagOnTheOverrideListIsTheResultLevelHistoryOnly(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    rows = [resultLevel(), resultLevel("crystals", fieldId=FIELD_ID)]
    db = ReviewDb(mkAr(), overrides=rows)
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        detail = await reviewService(db).getFullResult(RID, viewerRole="SUPERVISOR")
    assert [o["parameterName"] for o in detail["manualOverrides"]] == ["erythrocytes"]
    assert any("field_id IS NULL" in s for s in db.statements if "manual_overrides" in s)


@pytest.mark.asyncio
async def test_withTheFlagOffNothingMultiFieldIsAddedOrAsked(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    rows = [resultLevel(), resultLevel("crystals", fieldId=FIELD_ID)]
    db = ReviewDb(mkAr(status=ResultStatus.RETURNED_FOR_CORRECTION), overrides=rows,
                  returnRow=("Blurry image", None, None))
    builder = AsyncMock(return_value=[])
    with patch("src.services.result_review_service.buildFieldSummaries", builder):
        detail = await reviewService(db).getFullResult(RID, viewerRole="SUPERVISOR")

    builder.assert_not_awaited()
    assert not any(key in detail for key in (
        "fields", "fieldCount", "fieldSummary", "isLegacySingleField", "returnFieldId", "returnParticleType"))
    assert len(detail["manualOverrides"]) == 2                  # unfiltered, as before
    assert detail["returnReason"] == "Blurry image"
    assert detail["flaggedAnomalies"] == {"erythrocytes": "above"}
    assert detail["imageUrl"] == "https://signed.example/single"
    assert not any("field_id IS NULL" in s for s in db.statements)


@pytest.mark.asyncio
async def test_theLatestReturnsFieldAndParticleAreExposedWhileReturned(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReviewDb(mkAr(status=ResultStatus.RETURNED_FOR_CORRECTION, summary=None),
                  returnRow=("Check the red cells", FIELD_ID, "erythrocytes"))
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        detail = await reviewService(db).getFullResult(RID, viewerRole="SUPERVISOR")
    assert (detail["returnReason"], detail["returnFieldId"], detail["returnParticleType"]) == (
        "Check the red cells", FIELD_ID, "erythrocytes")
    model = FullResultDetail(**detail)
    assert model.returnFieldId == FIELD_ID and model.returnParticleType == "erythrocytes"


@pytest.mark.asyncio
async def test_noReturnPointerWhenTheResultIsNotReturned(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReviewDb(mkAr(), returnRow=("old reason", FIELD_ID, "erythrocytes"))
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        detail = await reviewService(db).getFullResult(RID, viewerRole="SUPERVISOR")
    assert detail["returnReason"] is None and detail["returnFieldId"] is None
    assert "returnFieldId" not in FullResultDetail(**detail).model_dump(mode="json")


@pytest.mark.asyncio
async def test_aReturnWithNoPointerOmitsTheKeys(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReviewDb(mkAr(status=ResultStatus.RETURNED_FOR_CORRECTION, summary=None), returnRow=("Blurry", None, None))
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        dumped = FullResultDetail(**await reviewService(db).getFullResult(RID, viewerRole="SUPERVISOR")).model_dump(mode="json")
    assert "returnFieldId" not in dumped and "returnParticleType" not in dumped
    assert dumped["returnReason"] == "Blurry" and dumped["fieldCount"] == 10


@pytest.mark.asyncio
async def test_aZeroFieldCountIsKeptNotOmitted(monkeypatch, signing):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    ar = mkAr(summary=None)
    ar.fieldCount = 0
    with patch("src.services.result_review_service.buildFieldSummaries", AsyncMock(return_value=[])):
        dumped = FullResultDetail(**await reviewService(ReviewDb(ar)).getFullResult(RID, viewerRole="SUPERVISOR")).model_dump(mode="json")
    assert dumped["fieldCount"] == 0 and dumped["fields"] == [] and dumped["isLegacySingleField"] is False


# ── return: clears the marks and points at a field ───────────────────────────

class ReturnDb:
    def __init__(self, owner=RID, transitioned=1, unverified=10):
        self.result = AnalysisResult(resultId=RID, specimenId=SID, status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
        self.specimen = Specimen(specimenId=SID, medtechId=UID, sampleUid="SMP-1", status="PROCESSING")
        self.owner = owner
        self.transitioned = transitioned
        self.unverified = unverified
        self.added: list = []
        self.statements: list[str] = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, model, key, **kwargs):
        return self.result if model is AnalysisResult else self.specimen

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        if low.startswith("select result_fields.result_id"):
            out.scalar_one_or_none.return_value = self.owner
        elif low.startswith("update analysis_results"):
            out.rowcount = self.transitioned
        elif low.startswith("update result_fields"):
            out.rowcount = self.unverified
        else:  # pragma: no cover
            raise AssertionError(f"unexpected statement: {low}")
        return out


def returnService(db):
    notif = MagicMock(notifyMedtechResultReturned=AsyncMock())
    return ResultReviewService(db=db, auditLogger=MagicMock(record=AsyncMock()), _notifService=notif)


@pytest.mark.asyncio
async def test_withTheFlagOnAReturnClearsEveryFieldsVerifiedMarkInTheSameTransaction(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb()
    service = returnService(db)

    await service.returnResult(RID, UID, "Please recount", MagicMock())

    updates = [s for s in db.statements if s.lower().startswith("update result_fields")]
    assert len(updates) == 1
    assert "verified_by" in updates[0] and "verified_at" in updates[0]
    assert "version" not in updates[0].lower() and "last_corrected_at" not in updates[0].lower()
    assert db.statements.index(updates[0]) > min(
        i for i, s in enumerate(db.statements) if s.lower().startswith("update analysis_results"))
    db.commit.assert_awaited_once()
    assert service.auditLogger.record.call_args.kwargs["detailJson"] == {
        "specimen_id": str(SID), "fields_unverified": 10}


@pytest.mark.asyncio
async def test_aReturnMayPointAtAFieldAndAParticleAndTheReasonIsStoredAsWritten(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb()
    service = returnService(db)

    response = await service.returnResult(
        RID, UID, "Look at the red cells again", MagicMock(), fieldId=FIELD_ID, particleType="erythrocytes")

    (stored,) = db.added
    assert isinstance(stored, ResultReturn)
    assert (stored.reason, stored.fieldId, stored.particleType) == (
        "Look at the red cells again", FIELD_ID, "erythrocytes")
    assert (response["fieldId"], response["particleType"]) == (FIELD_ID, "erythrocytes")
    detail = service.auditLogger.record.call_args.kwargs["detailJson"]
    assert detail["field_id"] == str(FIELD_ID) and "red cells" not in str(service.auditLogger.record.call_args)
    assert ReturnResponse(**response).model_dump(mode="json")["fieldId"] == str(FIELD_ID)


@pytest.mark.asyncio
async def test_aFieldPointerWithoutAParticleIsAllowed(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb()
    response = await returnService(db).returnResult(RID, UID, "Field 3 is blurry", MagicMock(), fieldId=FIELD_ID)
    assert db.added[0].particleType is None and response["particleType"] is None
    assert "particleType" not in ReturnResponse(**response).model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [OTHER_RESULT, None])
async def test_aFieldFromAnotherResultOrNoResultIsRefusedAndNothingIsWritten(monkeypatch, owner):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb(owner=owner)
    service = returnService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.returnResult(RID, UID, "reason", MagicMock(), fieldId=FIELD_ID, particleType="crystals")
    assert (excInfo.value.status_code, excInfo.value.errorCode) == (422, "FIELD_NOT_IN_RESULT")
    assert db.added == [] and not any("update" in s.lower()[:8] for s in db.statements)
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_aParticleNeedsAFieldAndMustBeKnown(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb()
    with pytest.raises(HTTPException) as noField:
        await returnService(db).returnResult(RID, UID, "r", MagicMock(), particleType="crystals")
    with pytest.raises(HTTPException) as unknown:
        await returnService(db).returnResult(RID, UID, "r", MagicMock(), fieldId=FIELD_ID, particleType="unicorns")
    assert (noField.value.status_code, noField.value.errorCode) == (422, "FIELD_REQUIRED_FOR_PARTICLE")
    assert (unknown.value.status_code, unknown.value.errorCode) == (422, "INVALID_PARTICLE_TYPE")
    assert db.added == []


@pytest.mark.asyncio
async def test_aLostRaceClearsNoMarksAndCommitsNothing(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    db = ReturnDb(transitioned=0)
    service = returnService(db)
    with pytest.raises(HTTPException) as excInfo:
        await service.returnResult(RID, UID, "r", MagicMock())
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    assert not any(s.lower().startswith("update result_fields") for s in db.statements)
    db.commit.assert_not_awaited()
    service.auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_withTheFlagOffTheRequestExtrasAreIgnoredAndNoMarkIsTouched(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    db = ReturnDb(owner=OTHER_RESULT)                      # would be refused if looked at
    service = returnService(db)

    response = await service.returnResult(
        RID, UID, "Blurry image", MagicMock(), fieldId=FIELD_ID, particleType="unicorns")

    (stored,) = db.added
    assert stored.fieldId is None and stored.particleType is None
    assert set(response) == {"resultId", "status", "returnedAt"}
    assert not any("result_fields" in s for s in db.statements)
    assert service.auditLogger.record.call_args.kwargs["detailJson"] == {"specimen_id": str(SID)}


def test_theReturnRouteHandsTheRequestExtrasToTheService(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    review = MagicMock(returnResult=AsyncMock(return_value={
        "resultId": RID, "status": "RETURNED_FOR_CORRECTION", "returnedAt": AT,
        "fieldId": FIELD_ID, "particleType": "crystals"}))
    app.dependency_overrides[getResultReviewService] = lambda: review
    login("SUPERVISOR")

    response = TestClient(app).post(f"/api/v1/results/{RID}/return", json={
        "reason": "Check crystals", "fieldId": str(FIELD_ID), "particleType": "crystals"})

    assert response.status_code == 200
    assert response.json()["fieldId"] == str(FIELD_ID) and response.json()["particleType"] == "crystals"
    kwargs = review.returnResult.await_args.kwargs
    assert (kwargs["fieldId"], kwargs["particleType"], kwargs["reason"]) == (FIELD_ID, "crystals", "Check crystals")


def test_aMalformedFieldIdIsRejectedAtTheBoundary():
    app.dependency_overrides[getResultReviewService] = lambda: MagicMock()
    login("SUPERVISOR")
    response = TestClient(app).post(f"/api/v1/results/{RID}/return", json={"reason": "r", "fieldId": "not-a-uuid"})
    assert response.status_code == 422


# ── GET /results/medtech/pending: the latest return's pointer ────────────────

def pendingRow(status="RETURNED_FOR_CORRECTION"):
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = uuid.uuid4()
    ar.specimenId = uuid.uuid4()
    ar.status = status
    spec = MagicMock(spec=Specimen)
    spec.specimenId = ar.specimenId
    spec.sampleUid = "SMP-1"
    spec.testType = "Urinalysis"
    spec.priorityLevel = "ROUTINE"
    spec.receivedAt = AT
    spec.patientUid = "PAT-000001"
    return ar, spec


def returnRow(resultId, reason, fieldId, particleType, minutes=0):
    ret = MagicMock(spec=ResultReturn)
    ret.resultId = resultId
    ret.reason = reason
    ret.fieldId = fieldId
    ret.particleType = particleType
    ret.returnedAt = AT + timedelta(minutes=minutes)
    return ret


def pendingDb(rows, returns, withFields):
    patient = MagicMock(spec=Patient)
    patient.patientUid = "PAT-000001"
    patient.dateOfBirth = None
    patient.sex = "F"
    patients = MagicMock()
    patients.scalars.return_value.all.return_value = [patient]
    returnsResult = MagicMock()
    returnsResult.scalars.return_value.all.return_value = returns
    effects = [
        MagicMock(scalar_one=MagicMock(return_value=len(rows))),
        MagicMock(all=MagicMock(return_value=rows)),
        returnsResult,
    ]
    if withFields:
        effects.append(MagicMock(all=MagicMock(return_value=[])))
    effects.append(patients)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=effects)
    return db


def confirmationService(db):
    return ResultConfirmationService(
        db=db, auditLogger=MagicMock(record=AsyncMock()), _smartDiagnosisService=MagicMock(), _notifService=MagicMock())


@pytest.mark.asyncio
async def test_theQueueItemCarriesTheLatestReturnsFieldAndParticle(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    returned, plain = pendingRow(), pendingRow("PENDING_CONFIRM")
    otherField = uuid.uuid4()
    returns = [   # newest first, as the query orders them
        returnRow(returned[0].resultId, "newest", FIELD_ID, "erythrocytes", minutes=5),
        returnRow(returned[0].resultId, "older", otherField, "crystals", minutes=1),
    ]
    db = pendingDb([returned, plain], returns, withFields=True)

    listing = await confirmationService(db)._queryPendingForMedtech(UID, 1, 20)

    first, second = listing["items"]
    assert (first["returnReason"], first["returnFieldId"], first["returnParticleType"]) == (
        "newest", FIELD_ID, "erythrocytes")
    assert "returnFieldId" not in second and "returnParticleType" not in second
    assert db.execute.await_count == 5            # count, rows, returns, fields, patients: nothing per row


@pytest.mark.asyncio
async def test_aReturnWithNoPointerGivesNoneWhichTheJsonOmits(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    returned = pendingRow()
    db = pendingDb([returned], [returnRow(returned[0].resultId, "Blurry", None, None)], withFields=True)

    listing = await confirmationService(db)._queryPendingForMedtech(UID, 1, 20)

    assert listing["items"][0]["returnFieldId"] is None
    dumped = MedtechPendingListResponse(**listing).model_dump(mode="json")["items"][0]
    assert "returnFieldId" not in dumped and dumped["returnReason"] == "Blurry"
    assert dumped["fieldCount"] == 0                     # the counts are still there


@pytest.mark.asyncio
async def test_withTheFlagOffThePointerIsNeverSent(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    returned = pendingRow()
    db = pendingDb([returned], [returnRow(returned[0].resultId, "Blurry", FIELD_ID, "crystals")], withFields=False)

    listing = await confirmationService(db)._queryPendingForMedtech(UID, 1, 20)

    item = listing["items"][0]
    assert "returnFieldId" not in item and "returnParticleType" not in item
    assert item["returnReason"] == "Blurry"
    assert db.execute.await_count == 4


def test_thePendingJsonStillMatchesItsGoldenWithTheFlagOff(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    listing = {"items": [
        {"resultId": uuid.UUID("00000000-0000-0000-0000-0000000004a1"),
         "specimenId": uuid.UUID("00000000-0000-0000-0000-0000000004a2"), "sampleUid": "SMP-20261027-00001",
         "testType": "Urinalysis", "priorityLevel": "ROUTINE",
         "receivedAt": datetime(2026, 10, 27, 1, 2, 3, tzinfo=UTC), "patientUid": "PAT-0001", "patientAge": 41,
         "patientSex": "F", "status": "PENDING_CONFIRM", "returnReason": None},
        {"resultId": uuid.UUID("00000000-0000-0000-0000-0000000004a4"),
         "specimenId": uuid.UUID("00000000-0000-0000-0000-0000000004a5"), "sampleUid": None, "testType": None,
         "priorityLevel": None, "receivedAt": None, "patientUid": "", "patientAge": None, "patientSex": None,
         "status": "RETURNED_FOR_CORRECTION", "returnReason": "Blurry image"}], "total": 2, "page": 1, "pageSize": 20}
    app.dependency_overrides[getConfirmationService] = lambda: MagicMock(
        listPendingForMedtech=AsyncMock(return_value=listing))
    login("MEDTECH")
    response = TestClient(app).get("/api/v1/results/medtech/pending")
    assert response.text == (FIXTURES / "medtech_pending_golden.json").read_text(encoding="utf-8").rstrip("\n")


# ── retirements: 410 APP_UPDATE_REQUIRED ─────────────────────────────────────

OVERRIDE_BODY = {"parameter": "erythrocytes", "correctedValue": 4, "rationale": "Recount"}
ANNOTATE_BODY = {"annotationNotes": "looks fine"}


def stubServices():
    override = MagicMock(overrideParameter=AsyncMock(return_value=SimpleNamespace(
        overrideId=uuid.uuid4(), resultId=RID, parameterName="erythrocytes", parameter="erythrocytes",
        originalAiValue="9.0", correctedValue="4.0", rationale="Recount", overriddenAt=AT,
        medtechId=UID, overriddenBy=UID, id=uuid.uuid4())))
    review = MagicMock(saveAnnotation=AsyncMock(return_value={
        "resultId": RID, "annotationNotes": "looks fine", "spatialAnnotations": None}))
    app.dependency_overrides[getOverrideService] = lambda: override
    app.dependency_overrides[getResultReviewService] = lambda: review
    return override, review


@pytest.mark.parametrize("role", ["MEDTECH", "SUPERVISOR"])
def test_theResultLevelOverrideIsRetiredForBothRolesWithTheFlagOn(monkeypatch, role):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    override, _ = stubServices()
    login(role)
    response = TestClient(app).post(f"/api/v1/results/{RID}/override", json=OVERRIDE_BODY)
    assert response.status_code == 410
    assert response.json() == {"error": {"code": "APP_UPDATE_REQUIRED", "message": "Update UroLens to continue"}}
    override.overrideParameter.assert_not_awaited()


def test_the410BeatsABodyValidationError(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    stubServices()
    login("MEDTECH")
    assert TestClient(app).post(f"/api/v1/results/{RID}/override", json={"nonsense": True}).status_code == 410
    assert TestClient(app).post(f"/api/v1/results/{RID}/override").status_code == 410


def test_aWrongRoleIsForbiddenBeforeTheRetirement(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    stubServices()
    login("PATIENT")
    assert TestClient(app).post(f"/api/v1/results/{RID}/override", json=OVERRIDE_BODY).status_code == 403
    assert TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json=ANNOTATE_BODY).status_code == 403


def test_noTokenIsStillUnauthorizedNotGone(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    stubServices()
    assert TestClient(app).post(f"/api/v1/results/{RID}/override", json=OVERRIDE_BODY).status_code in (401, 403)


@pytest.mark.parametrize("role", ["MEDTECH", "SUPERVISOR"])
def test_withTheFlagOffTheOverrideRouteWorksForBothRoles(monkeypatch, role):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    override, _ = stubServices()
    login(role)
    response = TestClient(app).post(f"/api/v1/results/{RID}/override", json=OVERRIDE_BODY)
    assert response.status_code == 200
    override.overrideParameter.assert_awaited_once()


def test_aMedtechAnnotationIsRetiredWithTheFlagOn(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    _, review = stubServices()
    login("MEDTECH")
    response = TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json=ANNOTATE_BODY)
    assert response.status_code == 410 and response.json()["error"]["code"] == "APP_UPDATE_REQUIRED"
    assert TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json={"nonsense": 1}).status_code == 410
    review.saveAnnotation.assert_not_awaited()


def test_aSupervisorAnnotationIsUnchangedWithTheFlagOn(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    _, review = stubServices()
    login("SUPERVISOR")
    response = TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json=ANNOTATE_BODY)
    assert response.status_code == 200
    review.saveAnnotation.assert_awaited_once()
    assert TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json={"nonsense": 1}).status_code == 422


def test_withTheFlagOffAMedtechCanStillAnnotate(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    _, review = stubServices()
    login("MEDTECH")
    assert TestClient(app).patch(f"/api/v1/results/{RID}/annotate", json=ANNOTATE_BODY).status_code == 200
    review.saveAnnotation.assert_awaited_once()


# ── readers: patient, physician, PDF ─────────────────────────────────────────

def _row(summary, legacy=False, aiFindings=None, particleClasses=None):
    row = MagicMock(spec=AnalysisResult)
    row.resultId = RID
    row.status = "RELEASED"
    row.patientId = UID
    row.specimenId = SID
    row.releasedAt = AT
    row.confirmedAt = AT
    row.interpretation = "Normal"
    row.medtechName = None
    row.aiFindings = aiFindings if aiFindings is not None else {"erythrocytes": 3.6, "urinary_casts": 0.3}
    row.particleClasses = particleClasses if particleClasses is not None else {"erythrocytes": 4, "urinary_casts": 1}
    row.smartDiagnosisUnavailable = False
    row.fieldSummary = summary
    row.isLegacySingleField = legacy
    row.flaggedAnomalies = {"erythrocytes": "above"}
    return row


async def patientDetail(row) -> PatientResultDetailResponse:
    spec = MagicMock(spec=Specimen)
    spec.specimenId = SID
    spec.medtechId = None
    spec.labRequestId = None
    resolved = MagicMock()
    resolved.scalar_one_or_none.return_value = UID
    db = AsyncMock()
    db.execute = AsyncMock(return_value=resolved)

    async def _get(model, entityId):
        return row if model is AnalysisResult else spec

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()
    db.commit = AsyncMock()
    request = MagicMock()
    request.client.host = "127.0.0.1"
    return await PatientResultService(db=db, auditLogger=MagicMock(record=AsyncMock())).getResultDetail(
        RID, UID, request)


def counts(detail, label):
    return next(p for p in detail.particleCounts if p.label == label)


@pytest.mark.asyncio
async def test_aMultiFieldResultShowsTheAveragePerFieldWithItsLabel(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await patientDetail(_row(storedSummary()))

    assert (detail.fieldCount, detail.countsLabel) == (10, "average per field (10 fields)")
    assert (counts(detail, "erythrocytes").count, counts(detail, "erythrocytes").displayValue) == (4, "4")
    assert (counts(detail, "urinary_casts").count, counts(detail, "urinary_casts").displayValue) == (1, "<1")
    assert counts(detail, "bacteria").displayValue == "0"          # not seen in any field
    dumped = detail.model_dump(mode="json")
    assert dumped["countsLabel"] == "average per field (10 fields)"
    assert all("displayValue" in p for p in dumped["particleCounts"])


@pytest.mark.asyncio
async def test_aSeenParticleNeverReadsZeroForThePatient(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await patientDetail(_row(storedSummary()))
    casts = counts(detail, "urinary_casts")
    assert casts.count >= 1 and casts.displayValue != "0"


@pytest.mark.asyncio
async def test_aResultWithoutAFieldSummaryIsShownExactlyAsBefore(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await patientDetail(_row(None))
    dumped = detail.model_dump(mode="json")
    assert "fieldCount" not in dumped and "countsLabel" not in dumped
    assert all("displayValue" not in p for p in dumped["particleCounts"])
    assert counts(detail, "erythrocytes").count == 4


@pytest.mark.asyncio
async def test_withTheFlagOffASummaryIsIgnored(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    flagOff = (await patientDetail(_row(storedSummary()))).model_dump_json()
    plain = (await patientDetail(_row(None))).model_dump_json()
    assert flagOff == plain


@pytest.mark.asyncio
async def test_aLegacyResultIsLabelledSingleImage(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await patientDetail(_row(None, legacy=True, aiFindings={"erythrocytes": 7}, particleClasses={}))
    assert (detail.fieldCount, detail.countsLabel) == (1, "single image")
    assert counts(detail, "erythrocytes").count == 7 and counts(detail, "erythrocytes").displayValue is None


@pytest.mark.asyncio
async def test_aMalformedSummaryFallsBackToTheOldOutput(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await patientDetail(_row({"perClass": "garbage"}))
    assert detail.countsLabel is None and counts(detail, "erythrocytes").displayValue is None


def physicianService(ar):
    spec = MagicMock(spec=Specimen)
    spec.specimenId = SID
    spec.patientUid = "PAT-1"
    spec.patientName = ""
    spec.medtechId = None
    spec.labRequestId = uuid.uuid4()
    lab = SimpleNamespace(physicianId=UID, labRequestId=spec.labRequestId)
    pat = MagicMock(spec=Patient)
    pat.patientId = UID
    pat.patientUid = "PAT-1"
    pat.firstName = pat.lastName = pat.dateOfBirth = None
    pat.sex = "F"
    ar.patientId = UID
    ar.imageId = None
    ar.modelVersion = "mvp-v1.0"
    ar.createdAt = AT

    async def _get(model, entityId):
        return {AnalysisResult: ar, Specimen: spec}.get(model) or (lab if model.__name__ == "LabRequest" else pat)

    sdo = MagicMock()
    sdo.scalars.return_value.all.return_value = []
    review = MagicMock()
    review.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(side_effect=[sdo, review])
    db.add = MagicMock()
    db.commit = AsyncMock()
    return PhysicianResultService(db=db, auditLogger=MagicMock(record=AsyncMock()))


async def physicianDetail(row) -> PhysicianResultDetail:
    request = MagicMock()
    request.client.host = "127.0.0.1"
    return await physicianService(row).getResultDetail(RID, UID, request)


@pytest.mark.asyncio
async def test_thePhysicianGetsTheSummaryTheLabelAndNoAnomalies(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await physicianDetail(_row(storedSummary()))
    assert (detail.fieldCount, detail.countsLabel) == (10, "average per field (10 fields)")
    assert detail.fieldSummary.perClass["urinary_casts"].displayValue == "<1"
    assert detail.fieldSummary.perClass["erythrocytes"].displayValue == "4"
    assert detail.flaggedAnomalies == {}


@pytest.mark.asyncio
async def test_thePhysicianKeepsTheOldOutputWithoutASummary(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await physicianDetail(_row(None))
    dumped = detail.model_dump(mode="json")
    assert not any(k in dumped for k in ("fieldCount", "countsLabel", "fieldSummary"))
    assert detail.flaggedAnomalies == {"erythrocytes": "above"}


@pytest.mark.asyncio
async def test_aLegacyResultIsLabelledSingleImageForThePhysician(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = await physicianDetail(_row(None, legacy=True))
    assert (detail.fieldCount, detail.countsLabel, detail.fieldSummary) == (1, "single image", None)
    assert detail.flaggedAnomalies == {"erythrocytes": "above"}


@pytest.mark.asyncio
async def test_withTheFlagOffThePhysicianSeesNothingNew(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    flagOff = (await physicianDetail(_row(storedSummary()))).model_dump_json()
    plain = (await physicianDetail(_row(None))).model_dump_json()
    assert flagOff == plain


def pdfText(detail: PatientResultDetail):
    paragraphs: list[str] = []
    tables: list = []

    def _paragraph(text, *args, **kwargs):
        paragraphs.append(text)
        return MagicMock()

    def _table(data, *args, **kwargs):
        tables.extend(data)
        return MagicMock()

    with patch("reportlab.platypus.Paragraph", side_effect=_paragraph), \
         patch("reportlab.platypus.Table", side_effect=_table), \
         patch("reportlab.platypus.SimpleDocTemplate.build", return_value=None):
        pdf_service.generateResultPdf(detail, "Jane Doe", uuid.uuid4())
    return paragraphs, tables


def _pdfRow(tables, name):
    return next(row for row in tables if name in str(row))


@pytest.mark.asyncio
async def test_thePdfPrintsTheAveragesTheLabelAndLessThanOne(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = PatientResultDetail(**(await patientDetail(_row(storedSummary()))).model_dump())

    paragraphs, tables = pdfText(detail)

    assert "Counts shown as: average per field (10 fields)" in paragraphs
    assert _pdfRow(tables, "Red Blood Cells (RBC)")[1] == "4"
    assert _pdfRow(tables, "Casts")[1] == "<1"
    assert _pdfRow(tables, "Bacteria")[1] == "0"
    assert paragraphs.index("Cell Count Results") + 1 == paragraphs.index("Counts shown as: average per field (10 fields)")


@pytest.mark.asyncio
async def test_thePdfLabelsALegacyResultAsSingleImage(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    detail = PatientResultDetail(**(await patientDetail(
        _row(None, legacy=True, aiFindings={"erythrocytes": 7}, particleClasses={}))).model_dump())
    paragraphs, tables = pdfText(detail)
    assert "Counts shown as: single image" in paragraphs
    assert _pdfRow(tables, "Red Blood Cells (RBC)")[1] == "7"


def test_thePdfIsAsBeforeWithoutAFieldSummary():
    detail = PatientResultDetail(**_patientDetail().model_dump())
    paragraphs, tables = pdfText(detail)
    assert not any(p.startswith("Counts shown as") for p in paragraphs)
    assert _pdfRow(tables, "Red Blood Cells (RBC)")[1] == "6" and _pdfRow(tables, "Bacteria")[1] == "0"


# ── one mapper: no other code reads the stored summary's keys ────────────────

def test_onlyTheMapperAndTheWriterTouchTheStoredSummaryKeys():
    root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "field_aggregation_service.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#")[0]
            if re.search(r"""\.fieldSummary\b""", code):
                allowed = "toContractFieldSummary(" in code or re.search(r"\.fieldSummary\s*=", code)
                if not allowed:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
            if re.search(r"""\[['"](perClass|focalFindings)['"]\]""", code) and "contract" not in code.lower() \
                    and path.name != "field_aggregation_service.py":
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []
