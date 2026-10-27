"""Unit tests — an image upload adds one field while the multi-field workflow is on.

- The first field creates the specimen's result in CAPTURING; later fields add to
  it with the next field number, never replacing an image or resetting anything.
- Each field keeps its own counts, boxes (percent of the image, backend ids),
  confidence, magnification and Smart Diagnosis preview.
- Only the assigned MedTech, on a live specimen whose result is absent or
  CAPTURING, may add a field.
- A failed analysis stores nothing. A box the engine returns that can't be drawn
  is dropped and counted; the AI counts stay as reported.
DB, storage and the AI engine are mocked.
"""
from __future__ import annotations

import math
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.audit_logger import AuditLogger
from src.core.exceptions import AIAnalysisError, ConflictException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import Image, ImageStatus
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.models.specimen import Specimen
from src.services import field_capture_service
from src.services.field_capture_service import FieldCaptureService, toFieldDetections

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a1")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a2")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a3")


def _specimen(status: str = "ASSIGNED") -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = MEDTECH_ID
    specimen.status = status
    specimen.labRequestId = None
    return specimen


def _capturingResult(fieldCount: int = 0) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.CAPTURING
    result.fieldCount = fieldCount
    result.smartDiagnosis = None
    return result


def _inference(**overrides: object) -> SimpleNamespace:
    values = {
        "particles": {"urinary-casts": 1, "erythrocytes": 3},
        "confidence_scores": {"urinary-casts": 0.9, "erythrocytes": 0.8},
        "image_width": 800,
        "image_height": 600,
        "detections": [SimpleNamespace(class_name="urinary-casts", confidence=0.9, bbox=[80, 60, 160, 120])],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(highestFieldNo: int | None = None) -> tuple[FieldCaptureService, AsyncMock]:
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=highestFieldNo)))

    async def _flush(objs: list) -> None:
        for obj in objs:
            if isinstance(obj, AnalysisResult) and obj.resultId is None:
                obj.resultId = RESULT_ID

    db.flush = AsyncMock(side_effect=_flush)
    auditLogger = MagicMock(spec=AuditLogger, record=AsyncMock())
    return FieldCaptureService(db=db, auditLogger=auditLogger), db


async def _upload(
    service: FieldCaptureService,
    existing: AnalysisResult | None,
    inference: SimpleNamespace,
    magnification: Magnification = Magnification.HPF,
    diagnosis: object = None,
):
    diagnosisPatch = (
        patch.object(FieldCaptureService, "_computeSmartDiagnosis", side_effect=diagnosis)
        if isinstance(diagnosis, Exception)
        else patch.object(FieldCaptureService, "_computeSmartDiagnosis", return_value=diagnosis or {"gout": {"level": "LOW"}})
    )
    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"raw"),
        _requireFieldUploadAllowed=AsyncMock(return_value=(_specimen(), existing)),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"cleaned"),
        _runInference=AsyncMock(return_value=inference),
        _uploadToStorage=AsyncMock(),
    ), diagnosisPatch, patch.object(
        field_capture_service.specimen_service, "markProcessing", AsyncMock(return_value=existing is None)
    ):
        response = await service.handleFieldUpload(
            SPECIMEN_ID, MEDTECH_ID, MagicMock(content_type="image/jpeg"), magnification, None
        )
        stored = service._uploadToStorage.await_args.args[0]
    return response, stored


def _added(db: AsyncMock, kind: type) -> list:
    return [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], kind)]


# ── The first field and later fields ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_theFirstFieldCreatesACapturingResultAndAnAnalyzedField() -> None:
    service, db = _service(highestFieldNo=None)

    response, stored = await _upload(service, None, _inference())

    [result] = _added(db, AnalysisResult)
    [field] = _added(db, ResultField)
    [image] = _added(db, Image)
    assert result.status == ResultStatus.CAPTURING
    assert result.imageId == image.imageId
    assert image.status == ImageStatus.ACTIVE
    assert stored == b"cleaned"
    assert (field.fieldNo, field.status, field.resultId) == (1, FieldStatus.ANALYZED, RESULT_ID)
    assert field.aiFindings == field.effectiveCounts == {"urinary_casts": 1, "erythrocytes": 3}
    assert field.confidenceScores == {"urinary_casts": 0.9, "erythrocytes": 0.8}
    assert response.fieldNo == 1
    assert response.status == FieldStatus.ANALYZED
    assert (response.acceptedFieldCount, response.minFields) == (0, 10)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aLaterFieldTakesTheNextNumberAndAddsToTheSameResult() -> None:
    service, db = _service(highestFieldNo=7)
    existing = _capturingResult(fieldCount=6)

    response, _ = await _upload(service, existing, _inference(), Magnification.LPF)

    assert _added(db, AnalysisResult) == []
    [field] = _added(db, ResultField)
    assert (field.fieldNo, field.resultId, field.magnification) == (8, RESULT_ID, Magnification.LPF)
    assert response.magnification == Magnification.LPF
    assert response.acceptedFieldCount == 6


@pytest.mark.asyncio
async def test_theUploadIsAuditedWithTheFieldItAdded() -> None:
    service, _ = _service()

    response, _ = await _upload(service, None, _inference(), Magnification.LPF)

    call = service.auditLogger.record.await_args.kwargs
    assert call["eventType"] == "IMAGE_UPLOADED"
    assert call["detailJson"]["field_id"] == str(response.fieldId)
    assert (call["detailJson"]["field_no"], call["detailJson"]["magnification"]) == (1, "LPF")
    assert call["detailJson"]["started_analysis"] is True


@pytest.mark.asyncio
async def test_aFailedAnalysisStoresNothing() -> None:
    service, db = _service()
    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"raw"),
        _requireFieldUploadAllowed=AsyncMock(return_value=(_specimen(), None)),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"cleaned"),
        _runInference=AsyncMock(side_effect=AIAnalysisError()),
        _uploadToStorage=AsyncMock(),
    ):
        with pytest.raises(AIAnalysisError):
            await service.handleFieldUpload(
                SPECIMEN_ID, MEDTECH_ID, MagicMock(content_type="image/jpeg"), Magnification.HPF, None
            )
        service._uploadToStorage.assert_not_awaited()

    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("highest", "expected"), [(None, 1), (1, 2), (9, 10)])
async def test_fieldNumbersAreNeverReused(highest: int | None, expected: int) -> None:
    # The highest number includes discarded fields, so a number always means one capture.
    service, _ = _service(highestFieldNo=highest)
    assert await service._nextFieldNo(RESULT_ID) == expected


# ── Smart Diagnosis preview ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_theSmartDiagnosisPreviewStaysOnTheField() -> None:
    service, db = _service()
    existing = _capturingResult()

    response, _ = await _upload(service, existing, _inference(), diagnosis={"gout": {"level": "HIGH"}})

    [field] = _added(db, ResultField)
    assert field.smartDiagnosis == response.smartDiagnosisPreview == {"gout": {"level": "HIGH"}}
    assert field.smartDiagnosisUnavailable is False
    assert existing.smartDiagnosis is None


@pytest.mark.asyncio
async def test_aSmartDiagnosisFailureMarksOnlyThatFieldUnavailable() -> None:
    service, db = _service()

    response, _ = await _upload(service, None, _inference(), diagnosis=RuntimeError("rule engine down"))

    [field] = _added(db, ResultField)
    assert field.smartDiagnosis is None
    assert field.smartDiagnosisUnavailable is True
    assert response.smartDiagnosisUnavailable is True
    service.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aFieldWithNothingDetectedHasNoPreview() -> None:
    service, db = _service()

    await _upload(service, None, _inference(particles={}, confidence_scores={}, detections=[]))

    [field] = _added(db, ResultField)
    assert (field.smartDiagnosis, field.smartDiagnosisUnavailable) == (None, False)


# ── Boxes ─────────────────────────────────────────────────────────────────────

def test_boxesAreStoredInPercentOfTheImageWithTheirOwnIds() -> None:
    inference = _inference(detections=[
        SimpleNamespace(class_name="urinary-casts", confidence=0.9, bbox=[80, 60, 160, 120]),
        SimpleNamespace(class_name="erythrocytes", confidence=0.7, bbox=[0, 0, 800, 600]),
    ])

    boxes, width, height, dropped = toFieldDetections(inference, 1, 1)

    assert (width, height, dropped) == (800, 600, 0)
    first, second = boxes
    assert first["particleType"] == "urinary_casts"
    assert (first["x"], first["y"], first["w"], first["h"]) == pytest.approx((10, 10, 10, 10))
    assert (second["x"], second["y"], second["w"], second["h"]) == pytest.approx((0, 0, 100, 100))
    assert first["id"] != second["id"]


def test_boxesReachingPastTheEdgeAreClippedToTheImage() -> None:
    inference = _inference(detections=[SimpleNamespace(class_name="yeast", confidence=0.5, bbox=[760, 570, 900, 700])])

    [box], _, _, dropped = toFieldDetections(inference, 1, 1)

    assert dropped == 0

    assert (box["x"] + box["w"], box["y"] + box["h"]) == pytest.approx((100, 100))


@pytest.mark.parametrize("detections", [None, "not-a-list"])
def test_anEngineWithoutDetectionsGivesNoBoxes(detections: object) -> None:
    assert tuple(toFieldDetections(_inference(detections=detections), 640, 480)) == ([], 800, 600, 0)


def test_theValidatedImageSizeIsUsedWhenTheEngineGivesNone() -> None:
    inference = _inference(image_width=None, image_height=0, detections=[])
    assert tuple(toFieldDetections(inference, 640, 480)) == ([], 640, 480, 0)


@pytest.mark.parametrize(
    ("bbox", "confidence"),
    [
        ([100, 100, 100, 200], 0.5),  # no width
        ([300, 300, 200, 400], 0.5),  # corners swapped
        ([900, 0, 950, 10], 0.5),  # wholly outside the image
        ([math.nan, 0, 10, 10], 0.5),
        ([0, 0, math.inf, 10], 0.5),
        ([0, 0, 10, 10], math.nan),
        (["left", 0, 10, 10], 0.5),
        ([0, 0, 10], 0.5),
    ],
)
def test_aBoxThatCannotBeDrawnIsDroppedAndCounted(bbox: list, confidence: float) -> None:
    inference = _inference(detections=[
        SimpleNamespace(class_name="yeast", confidence=confidence, bbox=bbox),
        SimpleNamespace(class_name="yeast", confidence=0.6, bbox=[0, 0, 80, 60]),
    ])

    boxes, _, _, dropped = toFieldDetections(inference, 1, 1)

    assert dropped == 1
    assert [b["particleType"] for b in boxes] == ["yeast"]


@pytest.mark.asyncio
async def test_droppedBoxesKeepTheAiCountsAndAreAudited() -> None:
    service, db = _service()
    inference = _inference(detections=[
        SimpleNamespace(class_name="urinary-casts", confidence=0.9, bbox=[80, 60, 160, 120]),
        SimpleNamespace(class_name="erythrocytes", confidence=0.8, bbox=[math.nan, 0, 10, 10]),
    ])

    response, _ = await _upload(service, None, inference)

    [field] = _added(db, ResultField)
    assert field.aiFindings == {"urinary_casts": 1, "erythrocytes": 3}
    assert [d.particleType for d in response.detections] == ["urinary_casts"]
    assert service.auditLogger.record.await_args.kwargs["detailJson"]["dropped_boxes"] == 1
    db.commit.assert_awaited_once()


# ── Who may add a field ───────────────────────────────────────────────────────

def _accessService(existing: AnalysisResult | None) -> FieldCaptureService:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)))
    return FieldCaptureService(db=db, auditLogger=MagicMock())


async def _checkAccess(service: FieldCaptureService, specimen: Specimen) -> tuple:
    with patch.object(field_capture_service, "getAssignedSpecimen", AsyncMock(return_value=specimen)), \
         patch.object(field_capture_service, "requireProcessingConsent", AsyncMock()):
        return await service._requireFieldUploadAllowed(SPECIMEN_ID, MEDTECH_ID, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [None, _capturingResult()])
async def test_aFieldMayBeAddedBeforeTheFirstOneOrWhileCapturing(existing: AnalysisResult | None) -> None:
    specimen = _specimen()
    assert await _checkAccess(_accessService(existing), specimen) == (specimen, existing)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [s for s in ResultStatus if s != ResultStatus.CAPTURING])
async def test_noFieldMayBeAddedToAResultThatIsNotCapturing(status: ResultStatus) -> None:
    existing = _capturingResult()
    existing.status = status

    with pytest.raises(ConflictException) as excInfo:
        await _checkAccess(_accessService(existing), _specimen())

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"


@pytest.mark.asyncio
async def test_noFieldMayBeAddedToARejectedSpecimen() -> None:
    with pytest.raises(ConflictException) as excInfo:
        await _checkAccess(_accessService(None), _specimen(status="REJECTED"))

    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"


# ── Consent ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_theCallersConsentCheckRunsForTheSpecimenBeingCaptured() -> None:
    service, specimen, consent = _accessService(None), _specimen(), AsyncMock()

    with patch.object(field_capture_service, "getAssignedSpecimen", AsyncMock(return_value=specimen)), \
         patch.object(field_capture_service, "requireProcessingConsent", consent):
        await service._requireFieldUploadAllowed(SPECIMEN_ID, MEDTECH_ID, None)

    consent.assert_awaited_once_with(
        service.db, specimen, MEDTECH_ID, service.auditLogger, action="IMAGE_UPLOAD", request=None
    )


@pytest.mark.asyncio
async def test_noFieldMayBeAddedForAPatientWhoRefusedProcessing() -> None:
    refused = ConflictException(code="CONSENT_REFUSED", message="The patient refused processing.")

    with patch.object(field_capture_service, "getAssignedSpecimen", AsyncMock(return_value=_specimen())), \
         patch.object(field_capture_service, "requireProcessingConsent", AsyncMock(side_effect=refused)), \
         pytest.raises(ConflictException) as excInfo:
        await _accessService(None)._requireFieldUploadAllowed(SPECIMEN_ID, MEDTECH_ID, None)

    assert excInfo.value.errorCode == "CONSENT_REFUSED"

