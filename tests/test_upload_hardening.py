"""Unit tests — AIIntegrationService upload guards (SEC-2).

Covers the three checks `handleUpload` now runs before an image is decoded
or stored (security audit F-04, F-05, F-09, F-18):
- `_readWithinLimit`: 10 MB cap, never reading more than one byte past it.
- `_requireUploadAllowed`: only the assigned MedTech, only on a live
  specimen, only while the result can still take a new image.
- `_validateImage`: Pillow limited to real JPEG/PNG whose content matches the
  declared type; no Pillow internals in the client-facing message.
DB is mocked; images are generated in memory.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import BmpImagePlugin, EpsImagePlugin, GifImagePlugin, TiffImagePlugin
from PIL import Image as PILImage

from src.core.exceptions import (
    ConflictException,
    ForbiddenException,
    ImageFormatError,
    ImageTooLargeError,
    SpecimenNotFoundError,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.specimen import Specimen
from src.services.ai_integration_service import MAX_IMAGE_BYTES, AIIntegrationService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000061")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000062")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000063")


def _imageBytes(fmt: str, size: tuple[int, int] = (800, 600)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color=(120, 180, 240)).save(buf, format=fmt)
    return buf.getvalue()


def _makeUpload(data: bytes, declaredSize: int | None = None) -> MagicMock:
    # Mirrors Starlette's UploadFile: .size may be known up front; .read(n)
    # returns at most n bytes.
    upload = MagicMock()
    upload.size = declaredSize
    upload.read = AsyncMock(side_effect=lambda n=-1: data if n < 0 else data[:n])
    return upload


def _makeSpecimen(medtechId: uuid.UUID = MEDTECH_ID, status: str = "PROCESSING") -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    specimen.status = status
    return specimen


def _makeService(specimen: Specimen | None = None, resultStatus: str | None = None) -> AIIntegrationService:
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    result = None
    if resultStatus is not None:
        result = MagicMock(spec=AnalysisResult)
        result.status = resultStatus
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=executeResult)
    return AIIntegrationService(db=db, auditLogger=MagicMock())


# ── Size cap (F-09) ───────────────────────────────────────────────────────────

def test_maxImageSizeIsTenMegabytes():
    assert MAX_IMAGE_BYTES == 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_uploadExactlyAtTheLimitIsAccepted():
    data = b"x" * MAX_IMAGE_BYTES
    assert await _makeService()._readWithinLimit(_makeUpload(data)) == data


@pytest.mark.asyncio
async def test_uploadOneByteOverTheLimitIsRejected():
    with pytest.raises(ImageTooLargeError) as excInfo:
        await _makeService()._readWithinLimit(_makeUpload(b"x" * (MAX_IMAGE_BYTES + 1)))

    assert excInfo.value.status_code == 413
    assert excInfo.value.errorCode == "IMAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_oversizedUploadIsRejectedFromItsSizeWithoutReadingIt():
    upload = _makeUpload(b"", declaredSize=MAX_IMAGE_BYTES + 1)

    with pytest.raises(ImageTooLargeError):
        await _makeService()._readWithinLimit(upload)

    upload.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_readNeverAsksForMoreThanOneBytePastTheLimit():
    upload = _makeUpload(b"x" * 10)

    await _makeService()._readWithinLimit(upload)

    upload.read.assert_awaited_once_with(MAX_IMAGE_BYTES + 1)


# ── Who may upload, and when (F-04) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_uploadIsAllowedForTheAssignedMedtechOnAFirstImage():
    await _makeService(specimen=_makeSpecimen())._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replaceableStatus",
    [
        ResultStatus.PENDING_CONFIRM,
        ResultStatus.RETURNED_FOR_CORRECTION,
        ResultStatus.IMAGE_RETAKE_REQUESTED,
        ResultStatus.FAILED,
    ],
)
async def test_uploadIsAllowedWhileTheResultCanStillTakeANewImage(replaceableStatus):
    service = _makeService(specimen=_makeSpecimen(), resultStatus=replaceableStatus)
    await service._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)


@pytest.mark.asyncio
async def test_uploadIsForbiddenOnAnotherMedtechsSpecimen():
    service = _makeService(specimen=_makeSpecimen(medtechId=OTHER_MEDTECH_ID))

    with pytest.raises(ForbiddenException) as excInfo:
        await service._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_uploadToAMissingSpecimenIsANotFoundNotAServerError():
    # Before SEC-2 this stored the file, then failed the images FK with a 500.
    with pytest.raises(SpecimenNotFoundError) as excInfo:
        await _makeService(specimen=None)._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.errorCode == "SPECIMEN_NOT_FOUND"


@pytest.mark.asyncio
async def test_uploadIsRefusedForARejectedSpecimen():
    service = _makeService(specimen=_makeSpecimen(status="REJECTED"))

    with pytest.raises(ConflictException) as excInfo:
        await service._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lockedStatus",
    [
        ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        ResultStatus.CRITICAL_ESCALATED,
        ResultStatus.APPROVED,
        ResultStatus.RELEASED,
    ],
)
async def test_uploadCannotResetASubmittedApprovedOrReleasedResult(lockedStatus):
    # An upload resets the result to PENDING_CONFIRM and wipes its findings.
    service = _makeService(specimen=_makeSpecimen(), resultStatus=lockedStatus)

    with pytest.raises(ConflictException) as excInfo:
        await service._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"


# ── What counts as an image (F-05, F-18) ──────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("fmt", "mime"), [("JPEG", "image/jpeg"), ("PNG", "image/png")])
async def test_genuineJpegAndPngAreAccepted(fmt, mime):
    assert await _makeService()._validateImage(_imageBytes(fmt), mime) == (800, 600)


@pytest.mark.asyncio
@pytest.mark.parametrize(("fmt", "declared"), [("PNG", "image/jpeg"), ("JPEG", "image/png")])
async def test_contentThatDoesNotMatchItsDeclaredTypeIsRejected(fmt, declared):
    with pytest.raises(ImageFormatError) as excInfo:
        await _makeService()._validateImage(_imageBytes(fmt), declared)

    assert excInfo.value.errorCode == "INVALID_IMAGE_FORMAT"
    assert "doesn't match" in excInfo.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("otherFormat", "plugin"),
    [
        ("GIF", GifImagePlugin.GifImageFile),
        ("BMP", BmpImagePlugin.BmpImageFile),
        ("TIFF", TiffImagePlugin.TiffImageFile),
    ],
)
async def test_otherFormatsLabelledAsJpegNeverReachTheirPillowParser(otherFormat, plugin):
    # Rejecting afterwards isn't enough: the risk is the parser running at
    # all (F-05). The spy proves Pillow never even tried it.
    data = _imageBytes(otherFormat)
    with patch.object(plugin, "_open", side_effect=AssertionError("parser ran")) as parserSpy:
        with pytest.raises(ImageFormatError):
            await _makeService()._validateImage(data, "image/jpeg")

    parserSpy.assert_not_called()


@pytest.mark.asyncio
async def test_epsFileLabelledAsJpegNeverReachesTheVulnerableEpsParser():
    # EPS has a Pillow 12.2.0 CVE (negative %%BeginBinary seek) reachable
    # from Image.open() — the exact path this restriction closes.
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 800 600\n%%BeginBinary: -1\n"
    with patch.object(
        EpsImagePlugin.EpsImageFile, "_open", side_effect=AssertionError("parser ran")
    ) as parserSpy:
        with pytest.raises(ImageFormatError):
            await _makeService()._validateImage(eps, "image/jpeg")

    parserSpy.assert_not_called()


@pytest.mark.asyncio
async def test_unreadableFileErrorDoesNotLeakPillowInternals():
    with pytest.raises(ImageFormatError) as excInfo:
        await _makeService()._validateImage(b"definitely not an image", "image/jpeg")

    assert excInfo.value.detail == "The file isn't a readable JPEG or PNG image."
    assert "PIL" not in excInfo.value.detail
    assert "cannot identify" not in excInfo.value.detail
