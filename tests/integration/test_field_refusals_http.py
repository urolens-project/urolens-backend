"""HTTP-level tests — every refusal the phone branches on, with its exact status and code.

The app maps these codes to its own messages (mobile PR #55, 3.10) and treats a
4xx other than 401/408/429 as permanent: a queued capture is discarded with the
server's message, a queued Proceed or Discard is reverted to "needs your decision".
So each code and status is API contract. Through the real routes and services, with
a fake database holding one specimen of the token's MedTech:

- capture (`POST /fields`): 409 `SPECIMEN_REJECTED`, 409 `RESULT_NOT_EDITABLE`,
  409 `CONSENT_REFUSED` (all before any analysis), 422 `IMAGE_NOT_MICROSCOPY` and
  `IMAGE_EXPOSURE` from the engine's gate, 503 `AI_ANALYSIS_FAILED` (the one the
  phone retries); nothing is committed;
- a Proceed or Discard replayed after the result left capture: 409
  `INVALID_RESULT_STATUS`, or 422 `REASON_REQUIRED` for a Discard while the result
  awaits the website (the phone never sends a reason); 409 `SPECIMEN_REJECTED` once
  the specimen is rejected.
Envelopes for the dedicated exception classes are in tests/test_field_error_codes.py.
"""
from __future__ import annotations

import io
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from PIL import Image as PILImage

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.core.exceptions import (
    AIAnalysisError,
    ImageExposureError,
    ImageNotMicroscopyError,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.consent import Consent
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.models.specimen import Specimen
from src.services.field_capture_service import FieldCaptureService
from tests.integration.conftest import MEDTECH_USER_ID

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000f101")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-00000000f102")
FIELD_ID = uuid.UUID("00000000-0000-0000-0000-00000000f103")
LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-00000000f104")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000f105")
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def _jpeg(width: int = 800, height: int = 600) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(200, 190, 170)).save(out, format="JPEG")
    return out.getvalue()


def _sample(
    specimenStatus: str = "PROCESSING",
    resultStatus: ResultStatus | None = ResultStatus.CAPTURING,
    consentProcess: bool | None = True,
) -> AsyncMock:
    """A session holding one specimen of the token's MedTech, its result (if any), one
    analysed field and the patient's latest consent (if any)."""
    specimen = Specimen(
        specimenId=SPECIMEN_ID, medtechId=MEDTECH_USER_ID, status=specimenStatus, labRequestId=LAB_REQUEST_ID
    )
    result = (
        None
        if resultStatus is None
        else AnalysisResult(resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=resultStatus)
    )
    field = ResultField(
        fieldId=FIELD_ID, resultId=RESULT_ID, specimenId=SPECIMEN_ID, imageId=uuid.uuid4(), fieldNo=1,
        magnification=Magnification.HPF, status=FieldStatus.ACCEPTED, aiFindings={}, effectiveCounts={},
        confidenceScores={}, imageWidth=800, imageHeight=600, version=1, createdAt=NOW, updatedAt=NOW,
    )
    consent = None if consentProcess is None else Consent(patientId=PATIENT_ID, consentProcess=consentProcess)

    db = AsyncMock()
    db.add = MagicMock()
    rows = {Specimen: specimen, AnalysisResult: result, ResultField: field}
    db.get = AsyncMock(side_effect=lambda model, _id, **_kw: rows.get(model))

    def _execute(statement: object, *_args: object, **_kwargs: object) -> MagicMock:
        selected = [c.get("entity") or c.get("type") for c in statement.column_descriptions]
        value = {(ResultField,): field, (AnalysisResult,): result}.get(tuple(selected), field.specimenId)
        answer = MagicMock()
        answer.scalar_one_or_none.return_value = value
        answer.scalar_one.return_value = value
        return answer

    db.execute = AsyncMock(side_effect=_execute)
    # The consent check: the lab request's patient, then that patient's latest consent.
    db.scalar = AsyncMock(side_effect=[PATIENT_ID, consent])
    return db


@pytest.fixture
def fieldsOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)


@pytest.fixture
def useDb(fieldsOn: None) -> AsyncGenerator:
    """Install a session built by `_sample` for one test."""
    installed: list[AsyncMock] = []

    def _install(db: AsyncMock) -> AsyncMock:
        async def _override() -> AsyncGenerator[AsyncMock]:
            yield db

        app.dependency_overrides[getDb] = _override
        installed.append(db)
        return db

    try:
        yield _install
    finally:
        app.dependency_overrides.pop(getDb, None)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _capture(asyncClient: AsyncClient, token: str) -> object:
    return await asyncClient.post(
        "/api/v1/fields",
        headers=_auth(token),
        data={"specimen_id": str(SPECIMEN_ID), "magnification": "HPF"},
        files={"file": ("field.jpg", _jpeg(), "image/jpeg")},
    )


def _assertRefused(response: object, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


# ── Capture: refused before any analysis ─────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sample", "status", "code"),
    [
        ({"specimenStatus": "REJECTED"}, 409, "SPECIMEN_REJECTED"),
        ({"resultStatus": ResultStatus.PENDING_CONFIRM}, 409, "RESULT_NOT_EDITABLE"),
        ({"resultStatus": ResultStatus.RETURNED_FOR_CORRECTION}, 409, "RESULT_NOT_EDITABLE"),
        ({"resultStatus": None, "consentProcess": False}, 409, "CONSENT_REFUSED"),
    ],
    ids=["rejected", "submitted", "returned-not-reopened", "consent-refused"],
)
async def test_aCaptureTheSampleCannotTakeIsRefusedBeforeAnalysis(
    asyncClient: AsyncClient, medtechToken: str, useDb, sample: dict, status: int, code: str
) -> None:
    db = useDb(_sample(**sample))

    with patch.object(FieldCaptureService, "_runInference", AsyncMock()) as inference:
        response = await _capture(asyncClient, medtechToken)

    _assertRefused(response, status, code)
    inference.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── Capture: refused by the engine ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ImageNotMicroscopyError(), 422, "IMAGE_NOT_MICROSCOPY"),
        (ImageExposureError(), 422, "IMAGE_EXPOSURE"),
        (AIAnalysisError(), 503, "AI_ANALYSIS_FAILED"),
    ],
    ids=["not-a-slide", "exposure", "engine-failed"],
)
async def test_aCaptureTheEngineRefusesStoresNothing(
    asyncClient: AsyncClient, medtechToken: str, useDb, error: Exception, status: int, code: str
) -> None:
    db = useDb(_sample(resultStatus=None))

    with patch.object(FieldCaptureService, "_runInference", AsyncMock(side_effect=error)), \
         patch.object(FieldCaptureService, "_uploadToStorage", AsyncMock()) as upload:
        response = await _capture(asyncClient, medtechToken)

    _assertRefused(response, status, code)
    upload.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── A queued Proceed or Discard replayed too late ────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resultStatus", "action", "status", "code"),
    [
        (ResultStatus.PENDING_CONFIRM, "accept", 409, "INVALID_RESULT_STATUS"),
        (ResultStatus.PENDING_SUPERVISOR_APPROVAL, "accept", 409, "INVALID_RESULT_STATUS"),
        (ResultStatus.PENDING_CONFIRM, "discard", 422, "REASON_REQUIRED"),
        (ResultStatus.PENDING_SUPERVISOR_APPROVAL, "discard", 409, "INVALID_RESULT_STATUS"),
    ],
)
async def test_aDecisionAfterTheResultLeftCaptureIsRefused(
    asyncClient: AsyncClient, medtechToken: str, useDb,
    resultStatus: ResultStatus, action: str, status: int, code: str,
) -> None:
    db = useDb(_sample(resultStatus=resultStatus))

    response = await asyncClient.post(f"/api/v1/fields/{FIELD_ID}/{action}", headers=_auth(medtechToken))

    _assertRefused(response, status, code)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "discard"])
async def test_aDecisionOnARejectedSpecimenIsRefused(
    asyncClient: AsyncClient, medtechToken: str, useDb, action: str
) -> None:
    db = useDb(_sample(specimenStatus="REJECTED"))

    response = await asyncClient.post(f"/api/v1/fields/{FIELD_ID}/{action}", headers=_auth(medtechToken))

    _assertRefused(response, 409, "SPECIMEN_REJECTED")
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aSubmitAfterTheResultLeftCaptureIsRefused(
    asyncClient: AsyncClient, medtechToken: str, useDb
) -> None:
    db = useDb(_sample(resultStatus=ResultStatus.PENDING_CONFIRM))

    response = await asyncClient.post(f"/api/v1/results/{RESULT_ID}/fields/submit", headers=_auth(medtechToken))

    _assertRefused(response, 409, "INVALID_RESULT_STATUS")
    db.commit.assert_not_awaited()
