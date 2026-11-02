"""Unit tests — a field capture through the real AI engine (no mocks for the engine).

The capture route's own steps run on real images with the pinned engine (v0.2.0):
validate → turn upright and strip metadata → infer → counts → boxes → Smart Diagnosis
preview. Everything else in the suite mocks the engine, so this is what catches an
engine change that breaks the boxes, the gate or the preview.

- A slide photo gives counts, one box per counted particle inside the image, the
  upright size, and a preview.
- A photo that isn't a slide is refused by the engine's gate as `IMAGE_NOT_MICROSCOPY`.
- The same slide stored sideways with an EXIF "rotate" tag gives the same boxes on
  the upright image (what keeps the phone's boxes on the cells, mobile PR #55, 3.2).

Skipped when the engine isn't installed. CI installs it, with its weights.
Images: `tests/fixtures/images/`, copied from urolens-ai-engine's test fixtures.
"""
from __future__ import annotations

import io
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from src.core.exceptions import ImageNotMicroscopyError
from src.services.field_capture_service import (
    FieldBoxes,
    FieldCaptureService,
    toFieldDetections,
)

pytest.importorskip("urolens_ai")

IMAGES = Path(__file__).parent / "fixtures" / "images"
SLIDE = IMAGES / "slide_leukocytes_640x480.jpg"
KITCHEN = IMAGES / "not_microscopy_kitchen.jpg"
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000e9e1")

# EXIF tag 0x0112: 6 means "stored turned 90° counter-clockwise; rotate clockwise to show".
EXIF_ORIENTATION = 0x0112
ROTATE_CLOCKWISE_TO_SHOW = 6
# Percentage points a box may move after the sideways photo is turned upright and
# re-encoded (JPEG is lossy).
BOX_TOLERANCE = 0.5


def _service() -> FieldCaptureService:
    return FieldCaptureService(db=AsyncMock(), auditLogger=MagicMock())


async def _capture(rawBytes: bytes) -> tuple[dict[str, int], FieldBoxes, FieldCaptureService]:
    """The capture route's image steps, in its order, without the database."""
    service = _service()
    width, height = await service._validateImage(rawBytes, "image/jpeg")
    upright = await service._stripMetadata(rawBytes, "image/jpeg")
    inference = await service._runInference(SPECIMEN_ID, upright)
    counts = service._countsFrom(inference, SPECIMEN_ID)
    return counts, toFieldDetections(inference, width, height), service


def _storedSideways(upright: bytes) -> bytes:
    """The same photo as a phone held sideways saves it: pixels turned, plus the tag."""
    image = Image.open(io.BytesIO(upright))
    sideways = image.rotate(90, expand=True)
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = ROTATE_CLOCKWISE_TO_SHOW
    out = io.BytesIO()
    sideways.save(out, format="JPEG", quality=95, exif=exif.tobytes())
    return out.getvalue()


@pytest.mark.asyncio
async def test_aSlidePhotoGivesCountsBoxesAndAPreview() -> None:
    counts, boxes, service = await _capture(SLIDE.read_bytes())

    assert counts and all(n > 0 for n in counts.values())
    assert (boxes.width, boxes.height) == (640, 480)
    assert boxes.dropped == 0
    assert Counter(b["particleType"] for b in boxes.boxes) == Counter(counts)
    for box in boxes.boxes:
        assert 0 <= box["x"] and box["x"] + box["w"] <= 100
        assert 0 <= box["y"] and box["y"] + box["h"] <= 100
        assert box["w"] > 0 and box["h"] > 0
        assert 0 < box["confidence"] <= 1
    preview, unavailable = service._previewSmartDiagnosis(counts, MagicMock())
    assert unavailable is False
    assert preview is not None


@pytest.mark.asyncio
async def test_aPhotoThatIsNotASlideIsRefusedByTheGate() -> None:
    with pytest.raises(ImageNotMicroscopyError) as refused:
        await _capture(KITCHEN.read_bytes())

    assert refused.value.errorCode == "IMAGE_NOT_MICROSCOPY"


@pytest.mark.asyncio
async def test_aSidewaysPhotoGivesTheSameBoxesOnTheUprightImage() -> None:
    sideways = _storedSideways(SLIDE.read_bytes())
    stored = Image.open(io.BytesIO(sideways))
    assert stored.size == (480, 640)
    assert stored.getexif()[EXIF_ORIENTATION] == ROTATE_CLOCKWISE_TO_SHOW

    _, upright, _ = await _capture(SLIDE.read_bytes())
    _, turned, _ = await _capture(sideways)

    assert (turned.width, turned.height) == (upright.width, upright.height)
    assert len(turned.boxes) == len(upright.boxes)

    def byPosition(box: dict) -> tuple:
        return (box["particleType"], round(box["x"]), round(box["y"]))

    for a, b in zip(sorted(upright.boxes, key=byPosition), sorted(turned.boxes, key=byPosition), strict=True):
        assert a["particleType"] == b["particleType"]
        for key in ("x", "y", "w", "h"):
            assert abs(a[key] - b[key]) <= BOX_TOLERANCE, (key, a, b)
