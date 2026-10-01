"""HTTP-level tests — capturing or uploading a microscopy image (UROLENS-230).

Through the real routes and services (DB and storage mocked):
- the upload stores the image without its metadata and returns
  `smartDiagnosisUnavailable`;
- a failed storage write is a 503 `STORAGE_ERROR` with nothing saved;
- "Begin Analysis" is audited;
- a result whose image was discarded is refused at confirm with `PENDING_RETAKE`.
"""
from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from PIL import Image as PILImage

from main import app
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.audit_log import AuditLog
from src.models.image import Image, ImageStatus
from src.models.specimen import Specimen
from src.services.ai_integration_service import AIIntegrationService
from tests.integration.conftest import MEDTECH_USER_ID, TEST_SPECIMEN_ID, _makeSbMock

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000003b1")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000003b2")


def _specimen(status: str = "PROCESSING") -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = TEST_SPECIMEN_ID
    specimen.medtechId = MEDTECH_USER_ID
    specimen.status = status
    specimen.labRequestId = None
    return specimen


def _jpegWithLocation() -> bytes:
    exif = PILImage.Exif()
    exif[0x0110] = "PhoneModel"
    exif.get_ifd(0x8825).update({1: "N", 2: (14.0, 35.0, 0.0)})
    buf = io.BytesIO()
    PILImage.new("RGB", (800, 600), (120, 180, 240)).save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _uploadDb() -> AsyncMock:
    """A first upload: the MedTech's own specimen, no result or image yet."""
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=_specimen())
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    async def _flush(objs: list) -> None:
        for obj in objs:
            if isinstance(obj, AnalysisResult) and obj.resultId is None:
                obj.resultId = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    return db


def _useDb(db: AsyncMock) -> None:
    async def _override() -> AsyncIterator[AsyncMock]:
        yield db

    app.dependency_overrides[getDb] = _override


def _inferReturning(particles: dict) -> object:
    import builtins

    realImport = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "urolens_ai":
            return MagicMock(infer=MagicMock(return_value=MagicMock(particles=particles)))
        return realImport(name, *args, **kwargs)

    return patch("builtins.__import__", _import)


async def _postUpload(asyncClient: AsyncClient, token: str, content: bytes) -> object:
    return await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("specimen.jpg", content, "image/jpeg")},
        data={"specimen_id": str(TEST_SPECIMEN_ID)},
    )


@pytest.mark.asyncio
async def test_uploadStoresTheImageWithoutItsLocationAndReportsTheDiagnosisFlag(
    asyncClient: AsyncClient, medtechToken: str
) -> None:
    db = _uploadDb()
    sb = _makeSbMock()
    _useDb(db)
    try:
        with patch("src.services.ai_integration_service.sb", sb), _inferReturning({"erythrocytes": 2}):
            response = await _postUpload(asyncClient, medtechToken, _jpegWithLocation())
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["smartDiagnosisUnavailable"] is False
    assert body["aiFindings"] == {"erythrocytes": 2}
    stored = sb.storage.from_.return_value.upload.await_args.kwargs["file"]
    assert not PILImage.open(io.BytesIO(stored)).getexif()
    assert b"PhoneModel" not in stored
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_uploadReportsWhenSmartDiagnosisIsUnavailable(asyncClient: AsyncClient, medtechToken: str) -> None:
    db = _uploadDb()
    _useDb(db)
    try:
        with patch("src.services.ai_integration_service.sb", _makeSbMock()), \
             _inferReturning({"erythrocytes": 2}), \
             patch.object(AIIntegrationService, "_runSmartDiagnosis", _failingDiagnosis):
            response = await _postUpload(asyncClient, medtechToken, _jpegWithLocation())
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 201, response.text
    assert response.json()["smartDiagnosisUnavailable"] is True


async def _failingDiagnosis(self: AIIntegrationService, result: AnalysisResult, findings: dict) -> None:
    # What the real method does when the engine fails.
    result.smartDiagnosisUnavailable = True


@pytest.mark.asyncio
async def test_aStorageFailureIsA503AndNothingIsSaved(asyncClient: AsyncClient, medtechToken: str) -> None:
    db = _uploadDb()
    sb = _makeSbMock()
    sb.storage.from_.return_value.upload = AsyncMock(side_effect=RuntimeError("bucket unreachable"))
    _useDb(db)
    try:
        with patch("src.services.ai_integration_service.sb", sb), _inferReturning({"erythrocytes": 2}):
            response = await _postUpload(asyncClient, medtechToken, _jpegWithLocation())
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "STORAGE_ERROR"
    added = [c.args[0] for c in db.add.call_args_list]
    assert not any(isinstance(obj, Image | AnalysisResult) for obj in added)
    db.commit.assert_not_awaited()  # the request's session rolls back


@pytest.mark.asyncio
async def test_beginAnalysisIsAudited(asyncClient: AsyncClient, medtechToken: str) -> None:
    db = AsyncMock()
    db.add = MagicMock()
    specimen = _specimen(status="ASSIGNED")
    db.get = AsyncMock(return_value=specimen)
    _useDb(db)
    try:
        response = await asyncClient.post(
            f"/api/v1/specimens/{TEST_SPECIMEN_ID}/start-analysis",
            headers={"Authorization": f"Bearer {medtechToken}"},
        )
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    assert specimen.status == "PROCESSING"
    [auditRow] = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert auditRow.eventType == "SPECIMEN_ANALYSIS_STARTED"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmingAResultWhoseImageWasDiscardedIsRefused(asyncClient: AsyncClient, medtechToken: str) -> None:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = TEST_SPECIMEN_ID
    result.status = ResultStatus.PENDING_CONFIRM
    result.imageId = IMAGE_ID
    result.manualOverrides = []
    discarded = MagicMock(spec=Image)
    discarded.status = ImageStatus.DISCARDED

    async def _get(model: type, *args: object, **kwargs: object) -> object:
        return discarded if model is Image else _specimen()

    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=result)))
    _useDb(db)
    try:
        response = await asyncClient.post(
            f"/api/v1/results/{RESULT_ID}/confirm", json={}, headers={"Authorization": f"Bearer {medtechToken}"}
        )
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PENDING_RETAKE"
    assert result.status == ResultStatus.PENDING_CONFIRM
    db.commit.assert_not_awaited()
