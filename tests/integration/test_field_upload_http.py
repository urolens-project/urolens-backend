"""HTTP-level tests — capturing a field, and the single-image upload it replaces.

- `POST /api/v1/fields` (flag on) adds a field and answers with its analysis;
  `magnification` reaches the service (HPF when omitted); an unknown one is a 422.
- `POST /api/v1/fields` (flag off) is 404 `FEATURE_DISABLED`.
- `POST /api/v1/images/upload` (flag on) is 410 `APP_UPDATE_REQUIRED` and stores
  nothing, so an app that predates multi-field is told to update.
- `POST /api/v1/images/upload` (flag off) answers exactly as before.
Services are replaced with fakes; their behaviour is covered by unit tests.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from main import app
from src.api.fields import getFieldCaptureService
from src.api.image import getAiIntegrationService
from src.core.config import settings
from src.models.analysis_result import ResultStatus
from src.models.result_field import FieldStatus, Magnification
from src.schemas.field import Detection, FieldAnalysisResponse
from tests.integration.conftest import MEDTECH_USER_ID, TEST_SPECIMEN_ID

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000004b1")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000004b2")
FIELD_ID = uuid.UUID("00000000-0000-0000-0000-0000000004b3")


def _fieldAnalysis(magnification: Magnification) -> FieldAnalysisResponse:
    return FieldAnalysisResponse(
        fieldId=FIELD_ID,
        fieldNo=3,
        magnification=magnification,
        resultId=RESULT_ID,
        imageId=IMAGE_ID,
        status=FieldStatus.ANALYZED,
        aiFindings={"urinary_casts": 1},
        detections=[Detection(id="d1", particleType="urinary_casts", confidence=0.9, x=10, y=10, w=10, h=10)],
        imageWidth=800,
        imageHeight=600,
        confidenceScores={"urinary_casts": 0.9},
        acceptedFieldCount=2,
        minFields=10,
    )


@pytest.fixture
def fakeServices():
    """Fake services for both upload routes, removed after the test."""
    fieldService = MagicMock()
    fieldService.handleFieldUpload = AsyncMock(side_effect=lambda *a, **k: _fieldAnalysis(a[3]))
    singleImageService = MagicMock()
    singleImageService.handleUpload = AsyncMock(return_value=MagicMock(
        resultId=RESULT_ID, specimenId=TEST_SPECIMEN_ID, imageId=IMAGE_ID,
        status=ResultStatus.PENDING_CONFIRM, aiFindings={"erythrocytes": 2}, flaggedAnomalies={},
        smartDiagnosis=None, smartDiagnosisUnavailable=False,
    ))
    app.dependency_overrides[getFieldCaptureService] = lambda: fieldService
    app.dependency_overrides[getAiIntegrationService] = lambda: singleImageService
    try:
        yield fieldService, singleImageService
    finally:
        app.dependency_overrides.pop(getFieldCaptureService, None)
        app.dependency_overrides.pop(getAiIntegrationService, None)


async def _post(asyncClient: AsyncClient, path: str, token: str, **form: str) -> object:
    return await asyncClient.post(
        path,
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("field.jpg", b"jpeg-bytes", "image/jpeg")},
        data={"specimen_id": str(TEST_SPECIMEN_ID), **form},
    )


# ── POST /api/v1/fields ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capturingAFieldAnswersWithItsAnalysis(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    fieldService, singleImageService = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)

    response = await _post(asyncClient, "/api/v1/fields", medtechToken, magnification="LPF")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["fieldId"], body["fieldNo"], body["magnification"]) == (str(FIELD_ID), 3, "LPF")
    assert body["detections"][0]["particleType"] == "urinary_casts"
    assert (body["acceptedFieldCount"], body["minFields"]) == (2, 10)
    args = fieldService.handleFieldUpload.await_args.args
    assert (args[0], args[1], args[3]) == (TEST_SPECIMEN_ID, MEDTECH_USER_ID, Magnification.LPF)
    singleImageService.handleUpload.assert_not_awaited()


@pytest.mark.asyncio
async def test_aFieldIsHpfWhenNoMagnificationIsSent(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    fieldService, _ = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)

    response = await _post(asyncClient, "/api/v1/fields", medtechToken)

    assert response.status_code == 201, response.text
    assert fieldService.handleFieldUpload.await_args.args[3] == Magnification.HPF


@pytest.mark.asyncio
async def test_anUnknownMagnificationIsRefusedBeforeAnyUpload(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    fieldService, _ = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)

    response = await _post(asyncClient, "/api/v1/fields", medtechToken, magnification="100X")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    fieldService.handleFieldUpload.assert_not_awaited()


@pytest.mark.asyncio
async def test_capturingAFieldIsUnavailableWithTheFlagOff(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    fieldService, _ = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)

    response = await _post(asyncClient, "/api/v1/fields", medtechToken)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FEATURE_DISABLED"
    fieldService.handleFieldUpload.assert_not_awaited()


# ── POST /api/v1/images/upload ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_theSingleImageUploadTellsAnOldAppToUpdateOnceFieldsAreOn(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    fieldService, singleImageService = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)

    response = await _post(asyncClient, "/api/v1/images/upload", medtechToken)

    assert response.status_code == 410
    assert response.json()["error"] == {"code": "APP_UPDATE_REQUIRED", "message": "Update UroLens to continue"}
    singleImageService.handleUpload.assert_not_awaited()
    fieldService.handleFieldUpload.assert_not_awaited()


@pytest.mark.asyncio
async def test_withTheFlagOffTheSingleImageUploadAnswersAsBefore(
    asyncClient: AsyncClient, medtechToken: str, fakeServices: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, singleImageService = fakeServices
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)

    response = await _post(asyncClient, "/api/v1/images/upload", medtechToken)

    assert response.status_code == 201, response.text
    assert response.json() == {
        "id": str(RESULT_ID),
        "resultId": str(RESULT_ID),
        "specimenId": str(TEST_SPECIMEN_ID),
        "imageId": str(IMAGE_ID),
        "status": "PENDING_CONFIRM",
        "aiFindings": {"erythrocytes": 2},
        "flaggedAnomalies": {},
        "smartDiagnosis": None,
        "smartDiagnosisUnavailable": False,
    }
    singleImageService.handleUpload.assert_awaited_once()
