"""Integration tests — T2.7: Image upload → AI inference hook → discard/retake flow

Covers
------
- Happy path: valid JPEG, 201 returned, analysis_results upserted.
- Format error: unsupported MIME type → 422.
- Resolution error: image below 640×480 → 422.
- AI inference triggered when urolens_ai package is importable.
- AI failure is non-fatal: upload still returns 201, findings stay empty.
- Discard flow: 200 returned, service validates state.
- Double-discard: 409 Conflict.

Architecture
------------
No real DB: Supabase client is replaced per-test with an AsyncMock.
See conftest.py for fixture details.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage

from main import app
from src.core.database import getDb
from src.core.exceptions import ConflictError, NotFoundError
from src.models.analysis_result import AnalysisResult
from src.models.image import Image
from src.services.image_retake_service import ImageRetakeService
from tests.integration.conftest import (
    MEDTECH_USER_ID,
    TEST_IMAGE_ID,
    TEST_SPECIMEN_ID,
    _makeSbMock,
)

# ── Image helpers ─────────────────────────────────────────────────────────────

def _makeJpeg(width: int = 800, height: int = 600) -> bytes:
    img = PILImage.new("RGB", (width, height), color=(120, 180, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


FAKE_AI_FINDINGS = {
    "RBC": 12,
    "WBC": 4,
    "Epithelial": 0,
    "Bacteria": 0,
    "Crystals": 0,
    "Casts": 0,
}


# ── SQLAlchemy AsyncSession mock (for the upload endpoint's Depends(getDb)) ────
#
# handleUpload() queries for a previous image/specimen/existing result (all
# absent on a first upload -- every .scalar_one_or_none() returns None to
# take the "create new" path), then constructs a real AnalysisResult(...)
# instance itself (not mocked) and flushes it. Unlike a real session, flush()
# on a mock never runs SQLAlchemy's column-default machinery, so `resultId`
# (mapped_column(..., default=uuid.uuid4)) is assigned by hand here, mirroring
# what a real flush would do.

def _makeSqlDbMock() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()  # real AsyncSession.add() is synchronous, not a coroutine
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=executeResult)

    async def _flush(objs):
        for obj in objs:
            if isinstance(obj, AnalysisResult) and obj.resultId is None:
                obj.resultId = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    return db


@pytest.fixture
def mockSqlDb():
    """Overrides Depends(getDb) app-wide for the duration of one test, so the
    upload endpoint's SQLAlchemy calls hit this mock instead of a real
    Postgres connection.
    """
    db = _makeSqlDbMock()

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


# ── Upload tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uploadValidImageReturns201(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """Valid 800×600 JPEG → 201 with PENDING_CONFIRM status."""
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg(800, 600)

    with patch("src.services.ai_integration_service.sb", sbMock):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["specimenId"] == str(testSpecimen)
    assert "resultId" in body
    assert "imageId" in body


@pytest.mark.asyncio
async def test_uploadUnsupportedFormatReturns422(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """GIF upload must be rejected with 422 — no Supabase calls needed."""
    gifBytes = b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"

    response = await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtechToken}"},
        files={"file": ("specimen.gif", gifBytes, "image/gif")},
        data={"specimenId": str(testSpecimen)},
    )

    assert response.status_code == 422
    assert "format" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_uploadUnsupportedFormatReturnsErrorCodeInResponseBody(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """HTTP-level proof for main.py's errorCode fix: `ImageFormatError`'s
    `INVALID_IMAGE_FORMAT` code (a third, distinct service) must reach the
    real JSON response body, not just the raised exception object.
    """
    gifBytes = b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"

    response = await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtechToken}"},
        files={"file": ("specimen.gif", gifBytes, "image/gif")},
        data={"specimenId": str(testSpecimen)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IMAGE_FORMAT"


@pytest.mark.asyncio
async def test_uploadBelowMinimumResolutionReturns422(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """320×240 JPEG is below 640×480 minimum — must return 422."""
    smallJpeg = _makeJpeg(320, 240)

    response = await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtechToken}"},
        files={"file": ("tiny.jpg", smallJpeg, "image/jpeg")},
        data={"specimenId": str(testSpecimen)},
    )

    assert response.status_code == 422
    assert "resolution" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_uploadTriggersAiInferenceWhenPackageAvailable(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """When urolens_ai is installed, _try_run_inference is called and the findings
    are included in the response (ai_findings populated).
    """
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg()

    # `infer` is the callable; `infer(raw_bytes)` returns the inference object
    # _runInference() reads `inferenceResult.particles` (a dict), not `.to_dict()`.
    mockInferFn = MagicMock()
    mockInferFn.return_value.particles = FAKE_AI_FINDINGS

    with (
        patch("src.services.ai_integration_service.sb", sbMock),
        # Simulate urolens_ai being installed by patching the import inside _try_run_inference
        patch("builtins.__import__", _makeImportMock("urolens_ai", "infer", mockInferFn)),
    ):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["aiFindings"] == FAKE_AI_FINDINGS


@pytest.mark.asyncio
async def test_uploadSucceedsWhenAiInferenceFails(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """AI failure must not break the upload. The endpoint returns 201 and
    ai_findings is None (the result row stays PENDING_CONFIRM with empty findings).
    """
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg()

    def _raisingInfer(_bytes):
        raise RuntimeError("GPU out of memory")

    with (
        patch("src.services.ai_integration_service.sb", sbMock),
        patch("builtins.__import__", _makeImportMock("urolens_ai", "infer", _raisingInfer)),
    ):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["aiFindings"] is None


# ── Discard tests ─────────────────────────────────────────────────────────────
#
# ImageRetakeService is now SQLAlchemy AsyncSession-injected (no more
# module-level `sb` to patch) — these construct the service directly with a
# mocked AsyncSession/AuditLogger, mirroring tests/test_lab_request_service.py's
# pattern, rather than driving the flow through the HTTP layer.

def _makeImageRow(imageStatus: str, imageId=None, specimenId=None) -> MagicMock:
    image = MagicMock(spec=Image)
    image.imageId = imageId or TEST_IMAGE_ID
    image.specimenId = specimenId or TEST_SPECIMEN_ID
    image.status = imageStatus
    image.discardedAt = None
    return image


def _makeRetakeDb(image: MagicMock | None) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(return_value=image)
    db.commit = AsyncMock()
    return db


def _makeRetakeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


@pytest.mark.asyncio
async def test_discardActiveImageReturns200() -> None:
    """Discarding an ACTIVE image succeeds: status flips to DISCARDED and is
    committed, and an IMAGE_DISCARDED audit entry is written via the
    centralized AuditLogger (not a raw insert).
    """
    image = _makeImageRow("ACTIVE")
    db = _makeRetakeDb(image)
    auditLogger = _makeRetakeAuditLogger()
    service = ImageRetakeService(db=db, auditLogger=auditLogger)

    result = await service.discardAndRetake(TEST_IMAGE_ID, MEDTECH_USER_ID, request=None)

    assert result["status"] == "DISCARDED"
    assert result["imageId"] == str(TEST_IMAGE_ID)
    assert result["discardedAt"] is not None
    assert image.status == "DISCARDED"
    db.commit.assert_awaited_once()

    auditLogger.record.assert_awaited_once()
    assert auditLogger.record.call_args.kwargs["eventType"] == "IMAGE_DISCARDED"
    assert auditLogger.record.call_args.kwargs["detailJson"] == {
        "specimen_id": str(TEST_SPECIMEN_ID)
    }


@pytest.mark.asyncio
async def test_discardAlreadyDiscardedImageReturns409() -> None:
    """Discarding an already-DISCARDED image must raise ConflictError (409)."""
    image = _makeImageRow("DISCARDED")
    db = _makeRetakeDb(image)
    service = ImageRetakeService(db=db, auditLogger=_makeRetakeAuditLogger())

    with pytest.raises(ConflictError) as excInfo:
        await service.discardAndRetake(TEST_IMAGE_ID, MEDTECH_USER_ID, request=None)

    assert excInfo.value.status_code == 409
    assert "discarded" in str(excInfo.value.detail).lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_discardNonexistentImageReturns404() -> None:
    """Discarding an image that doesn't exist must raise NotFoundError (404)."""
    db = _makeRetakeDb(None)
    service = ImageRetakeService(db=db, auditLogger=_makeRetakeAuditLogger())

    with pytest.raises(NotFoundError) as excInfo:
        await service.discardAndRetake(uuid.uuid4(), MEDTECH_USER_ID, request=None)

    assert excInfo.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_discardReplacedImageReturns409() -> None:
    """Discarding a REPLACED image must raise ConflictError (409)."""
    image = _makeImageRow("REPLACED")
    db = _makeRetakeDb(image)
    service = ImageRetakeService(db=db, auditLogger=_makeRetakeAuditLogger())

    with pytest.raises(ConflictError) as excInfo:
        await service.discardAndRetake(TEST_IMAGE_ID, MEDTECH_USER_ID, request=None)

    assert excInfo.value.status_code == 409
    db.commit.assert_not_awaited()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _makeImportMock(moduleName: str, attr: str, value):
    """Returns a replacement for builtins.__import__ that intercepts imports of
    `module_name` and returns a mock module exposing `attr` = `value`.

    All other imports fall through to the real __import__.
    """
    import builtins
    realImport = builtins.__import__

    def _patchedImport(name, *args, **kwargs):
        if name == moduleName:
            mod = MagicMock()
            setattr(mod, attr, value)
            return mod
        return realImport(name, *args, **kwargs)

    return _patchedImport
