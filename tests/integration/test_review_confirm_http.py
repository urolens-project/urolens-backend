"""HTTP-level tests — review and confirm a result on mobile (UROLENS-226).

Through the real routes and the real `ResultReviewService` (DB mocked):
`GET /results/{id}` gives a MedTech the return reason but no patient name;
`PATCH /results/{id}/annotate` enforces ownership and status rules with the
error envelope; `POST /results/{id}/confirm` returns `resubmitted` and `status`.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from main import app
from src.api.results import getConfirmationService, getResultReviewService
from src.core.config import settings
from src.core.encryption import encryptPii
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.patient import Patient
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen
from src.schemas.result_review import ConfirmResultResponse
from src.services.result_review_service import ResultReviewService
from tests.integration.conftest import MEDTECH_USER_ID

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f3")


def _token(role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "user_id": str(MEDTECH_USER_ID if role == "MEDTECH" else uuid.uuid4()),
            "role": role,
            "session_id": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwtSigningKey,
        algorithm=settings.jwtAlgorithm,
    )


def _headers(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role)}"}


def _result(status: str) -> AnalysisResult:
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


def _specimen(medtechId: uuid.UUID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    specimen.patientUid = "PAT-000001"
    specimen.patientName = None
    return specimen


def _db(result: AnalysisResult, specimen: Specimen) -> AsyncMock:
    """Answers `db.get` by model and `db.execute` by the entity selected."""
    patient = SimpleNamespace(
        firstName=encryptPii("Juan"), lastName=encryptPii("Dela Cruz"), dateOfBirth=None, sex="M"
    )
    byModel = {AnalysisResult: result, Specimen: specimen}
    byEntity = {AnalysisResult: result, Patient: patient, ResultReturn: "Recount the casts"}

    async def _get(model, *args, **kwargs):
        return byModel.get(model, SimpleNamespace(username="medtech1"))

    def _execute(stmt, *args, **kwargs):
        executeResult = MagicMock()
        executeResult.scalars.return_value.all.return_value = []
        executeResult.scalar_one_or_none.return_value = byEntity.get(stmt.column_descriptions[0].get("entity"))
        return executeResult

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    return db


def _useReviewService(db: AsyncMock) -> None:
    app.dependency_overrides[getResultReviewService] = lambda: ResultReviewService(
        db=db, auditLogger=MagicMock(record=AsyncMock())
    )


@pytest.fixture
def _clearOverrides():
    yield
    app.dependency_overrides.pop(getResultReviewService, None)
    app.dependency_overrides.pop(getConfirmationService, None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clearOverrides")
async def test_medtechReviewShowsTheReturnReasonAndNoPatientName(asyncClient) -> None:
    _useReviewService(_db(_result(ResultStatus.RETURNED_FOR_CORRECTION), _specimen(MEDTECH_USER_ID)))

    response = await asyncClient.get(f"/api/v1/results/{RESULT_ID}", headers=_headers("MEDTECH"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["returnReason"] == "Recount the casts"
    assert body["patientName"] is None
    assert body["patientUid"] == "PAT-000001"
    assert "Juan" not in response.text and "gAAAAA" not in response.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clearOverrides")
async def test_supervisorReviewKeepsThePatientName(asyncClient) -> None:
    _useReviewService(_db(_result(ResultStatus.PENDING_SUPERVISOR_APPROVAL), _specimen(MEDTECH_USER_ID)))

    response = await asyncClient.get(f"/api/v1/results/{RESULT_ID}", headers=_headers("SUPERVISOR"))

    assert response.status_code == 200, response.text
    assert response.json()["patientName"] == "Juan Dela Cruz"
    assert response.json()["returnReason"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clearOverrides")
@pytest.mark.parametrize(
    ("status", "assignedTo", "expectedStatus", "expectedCode"),
    [
        (ResultStatus.PENDING_CONFIRM, OTHER_MEDTECH_ID, 403, "SPECIMEN_NOT_ASSIGNED"),
        (ResultStatus.PENDING_SUPERVISOR_APPROVAL, MEDTECH_USER_ID, 409, "RESULT_NOT_EDITABLE"),
        (ResultStatus.RELEASED, MEDTECH_USER_ID, 422, "RESULT_ALREADY_FINALISED"),
    ],
)
async def test_medtechAnnotateIsRefusedWithTheErrorEnvelope(
    asyncClient, status, assignedTo, expectedStatus, expectedCode
) -> None:
    db = _db(_result(status), _specimen(assignedTo))
    _useReviewService(db)

    response = await asyncClient.patch(
        f"/api/v1/results/{RESULT_ID}/annotate",
        json={"annotationNotes": "Possible cast cluster"},
        headers=_headers("MEDTECH"),
    )

    assert response.status_code == expectedStatus, response.text
    assert response.json()["error"]["code"] == expectedCode
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clearOverrides")
async def test_medtechAnnotatesTheirOwnPendingResult(asyncClient) -> None:
    db = _db(_result(ResultStatus.PENDING_CONFIRM), _specimen(MEDTECH_USER_ID))
    _useReviewService(db)

    response = await asyncClient.patch(
        f"/api/v1/results/{RESULT_ID}/annotate",
        json={"annotationNotes": "Possible cast cluster"},
        headers=_headers("MEDTECH"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["annotationNotes"] == "Possible cast cluster"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clearOverrides")
async def test_confirmResponseCarriesResubmittedAndStatus(asyncClient) -> None:
    service = MagicMock()
    service.confirmResult = AsyncMock(return_value=ConfirmResultResponse(
        id=uuid.uuid4(), resultId=RESULT_ID, confirmedBy=MEDTECH_USER_ID, confirmedAt=datetime.now(UTC),
        resubmitted=True, status=ResultStatus.PENDING_SUPERVISOR_APPROVAL,
    ))
    app.dependency_overrides[getConfirmationService] = lambda: service

    response = await asyncClient.post(f"/api/v1/results/{RESULT_ID}/confirm", json={}, headers=_headers("MEDTECH"))

    assert response.status_code == 200, response.text
    assert response.json()["resubmitted"] is True
    assert response.json()["status"] == "PENDING_SUPERVISOR_APPROVAL"
    assert service.confirmResult.await_args.kwargs["medtechId"] == MEDTECH_USER_ID
