"""Unit tests — the AI engine's input gate and upright storage of uploads.

- The engine's gate codes become retake errors: `NOT_MICROSCOPY` is 422
  `IMAGE_NOT_MICROSCOPY`, `IMAGE_EXPOSURE` is 422 `IMAGE_EXPOSURE`; any other
  engine failure stays 503 `AI_ANALYSIS_FAILED`. Nothing is stored either way,
  on the single-image upload and on a field capture.
- A photo whose EXIF orientation isn't upright is stored upright (pixels
  turned, tag gone), so the AI's boxes line up with the stored image; its
  validated size is the upright one.
DB, storage and the engine are mocked; images are generated in memory.
"""
from __future__ import annotations

import builtins
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage

from src.core.exceptions import (
    AIAnalysisError,
    ImageExposureError,
    ImageNotMicroscopyError,
    ImageResolutionError,
)
from src.models.result_field import Magnification
from src.models.specimen import Specimen
from src.services.ai_integration_service import AIIntegrationService
from src.services.field_capture_service import FieldCaptureService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000005a2")
RED, BLUE = (220, 20, 20), (20, 20, 220)


class _EngineError(Exception):
    """Stands in for urolens_ai's ImageValidationError / InferenceError."""

    def __init__(self, code: str) -> None:
        super().__init__(f"[{code}] engine message")
        self.code = code


def _engineRaising(exc: Exception):
    realImport = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "urolens_ai":
            return MagicMock(infer=MagicMock(side_effect=exc))
        return realImport(name, *args, **kwargs)

    return patch("builtins.__import__", _import)


def _service(cls: type = AIIntegrationService) -> AIIntegrationService:
    db = AsyncMock()
    db.add = MagicMock()
    return cls(db=db, auditLogger=MagicMock(record=AsyncMock()))


def _specimen() -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = MEDTECH_ID
    specimen.status = "PROCESSING"
    specimen.labRequestId = None
    return specimen


# ── Gate codes become retake errors ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "error", "errorCode"),
    [
        ("NOT_MICROSCOPY", ImageNotMicroscopyError, "IMAGE_NOT_MICROSCOPY"),
        ("IMAGE_EXPOSURE", ImageExposureError, "IMAGE_EXPOSURE"),
    ],
)
async def test_aGateRejectionIsARetakeError(code: str, error: type, errorCode: str) -> None:
    with _engineRaising(_EngineError(code)), pytest.raises(error) as excInfo:
        await _service()._runInference(SPECIMEN_ID, b"jpeg")

    assert excInfo.value.status_code == 422
    assert excInfo.value.errorCode == errorCode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [_EngineError("MODEL_NOT_LOADED"), _EngineError("RESOLUTION_TOO_LOW"), RuntimeError("boom")],
)
async def test_anyOtherEngineFailureStaysAnAnalysisFailure(exc: Exception) -> None:
    with _engineRaising(exc), pytest.raises(AIAnalysisError) as excInfo:
        await _service()._runInference(SPECIMEN_ID, b"jpeg")

    assert excInfo.value.errorCode == "AI_ANALYSIS_FAILED"


@pytest.mark.asyncio
async def test_aGateRejectionOnTheSingleImageUploadStoresNothing() -> None:
    service = _service()
    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"raw"),
        _requireUploadAllowed=AsyncMock(return_value=_specimen()),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"cleaned"),
        _uploadToStorage=AsyncMock(),
    ), _engineRaising(_EngineError("NOT_MICROSCOPY")):
        with pytest.raises(ImageNotMicroscopyError):
            await service.handleUpload(SPECIMEN_ID, MEDTECH_ID, MagicMock(content_type="image/jpeg"), None)
        service._uploadToStorage.assert_not_awaited()

    service.db.add.assert_not_called()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aGateRejectionOnAFieldCaptureStoresNothing() -> None:
    service = _service(FieldCaptureService)
    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"raw"),
        _requireFieldUploadAllowed=AsyncMock(return_value=(_specimen(), None)),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"cleaned"),
        _uploadToStorage=AsyncMock(),
    ), _engineRaising(_EngineError("IMAGE_EXPOSURE")):
        with pytest.raises(ImageExposureError):
            await service.handleFieldUpload(
                SPECIMEN_ID, MEDTECH_ID, MagicMock(content_type="image/jpeg"), Magnification.HPF, None
            )
        service._uploadToStorage.assert_not_awaited()

    service.db.add.assert_not_called()
    service.db.commit.assert_not_awaited()


# ── Upright storage ───────────────────────────────────────────────────────────

def _halves(width: int = 800, height: int = 600) -> PILImage.Image:
    """Left half red, right half blue, so a rotation is visible in the pixels."""
    img = PILImage.new("RGB", (width, height), BLUE)
    img.paste(RED, (0, 0, width // 2, height))
    return img


def _encoded(fmt: str, orientation: int | None, width: int = 800, height: int = 600) -> bytes:
    exif = PILImage.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    buf = io.BytesIO()
    options = {"quality": 92} if fmt == "JPEG" else {}
    _halves(width, height).save(buf, format=fmt, exif=exif, **options)
    return buf.getvalue()


def _isRed(pixel: tuple) -> bool:
    return pixel[0] > 150 and pixel[2] < 100


def _isBlue(pixel: tuple) -> bool:
    return pixel[2] > 150 and pixel[0] < 100


@pytest.mark.asyncio
@pytest.mark.parametrize(("fmt", "mime"), [("JPEG", "image/jpeg"), ("PNG", "image/png")])
async def test_aSidewaysPhotoIsStoredUpright(fmt: str, mime: str) -> None:
    # Orientation 6 = "rotate 90° clockwise to display": the left (red) half ends up on top.
    cleaned = await _service()._stripMetadata(_encoded(fmt, 6), mime)

    stored = PILImage.open(io.BytesIO(cleaned)).convert("RGB")
    assert stored.size == (600, 800)
    assert _isRed(stored.getpixel((300, 100))) and _isBlue(stored.getpixel((300, 700)))
    assert 0x0112 not in PILImage.open(io.BytesIO(cleaned)).getexif()


@pytest.mark.asyncio
async def test_anUpsideDownPhotoIsTurnedWithoutSwappingItsSize() -> None:
    cleaned = await _service()._stripMetadata(_encoded("JPEG", 3), "image/jpeg")

    stored = PILImage.open(io.BytesIO(cleaned)).convert("RGB")
    assert stored.size == (800, 600)
    assert _isBlue(stored.getpixel((100, 300))) and _isRed(stored.getpixel((700, 300)))


@pytest.mark.asyncio
async def test_anUprightPhotoIsLeftAsItIs() -> None:
    original = _encoded("JPEG", 1)

    cleaned = await _service()._stripMetadata(original, "image/jpeg")

    stored = PILImage.open(io.BytesIO(cleaned))
    assert stored.size == (800, 600)
    assert stored.quantization == PILImage.open(io.BytesIO(original)).quantization


@pytest.mark.asyncio
async def test_validationReportsTheUprightSize() -> None:
    assert await _service()._validateImage(_encoded("JPEG", 6, 1000, 800), "image/jpeg") == (800, 1000)
    assert await _service()._validateImage(_encoded("JPEG", 3), "image/jpeg") == (800, 600)


@pytest.mark.asyncio
async def test_theResolutionCheckUsesTheUprightSize() -> None:
    # 640x480 stored as "rotate 90°" is 480x640 upright, which is what the engine checks.
    with pytest.raises(ImageResolutionError):
        await _service()._validateImage(_encoded("JPEG", 6, 640, 480), "image/jpeg")
