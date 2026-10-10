"""Unit tests — capturing or uploading a microscopy image (UROLENS-230).

- A retake resets everything derived from the old image (findings, Smart
  Diagnosis column/flag/output row, model version) and keeps a returned
  result RETURNED_FOR_CORRECTION.
- AI analysis that fails raises `AI_ANALYSIS_FAILED`; findings are normalized.
- Metadata (EXIF, PNG text) is stripped on the server; JPEG quality is kept.
- The upload starts a not-yet-started specimen, as "Begin Analysis" does.
- A confirmation updates the result's one Smart Diagnosis row instead of
  inserting a second (UNIQUE `result_id`); a failure flags any earlier one.
- A result whose image was discarded can't be confirmed (`PENDING_RETAKE`).
- The model weights default to those bundled in the `urolens_ai` package.
DB is mocked; images are generated in memory.
"""
from __future__ import annotations

import io
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage
from PIL import PngImagePlugin
from sqlalchemy import Delete, Update

from src.core import config
from src.core.audit_logger import AuditLogger
from src.core.exceptions import (
    AIAnalysisError,
    ImageFormatError,
    ImageTooLargeError,
    StorageError,
    UnprocessableException,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import Image, ImageStatus
from src.models.smart_diagnosis_output import SmartDiagnosisOutput
from src.models.specimen import Specimen
from src.services import ai_integration_service
from src.services.ai_integration_service import AIIntegrationService, InferenceOutput
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.smart_diagnosis_service import SmartDiagnosisService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a3")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000003a4")


def _makeSpecimen(status: str = "PROCESSING") -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = MEDTECH_ID
    specimen.status = status
    specimen.labRequestId = None
    return specimen


def _makeExistingResult(status: str) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.aiFindings = {"erythrocytes": 9}
    result.aiDetections = [{"id": "old-box"}]
    result.flaggedAnomalies = {"erythrocytes": 9}
    result.particleClasses = {"erythrocytes": 9}
    result.smartDiagnosis = {"gout": {"level": "HIGH"}}
    result.smartDiagnosisUnavailable = True
    result.modelVersion = "old-model"
    result.patientId = uuid.uuid4()
    return result


def _serviceReturning(existing: AnalysisResult | None) -> tuple[AIIntegrationService, AsyncMock]:
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)))
    return AIIntegrationService(db=db, auditLogger=MagicMock()), db


# ── A retake resets everything derived from the old image ─────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (ResultStatus.PENDING_CONFIRM, ResultStatus.PENDING_CONFIRM),
        (ResultStatus.RETURNED_FOR_CORRECTION, ResultStatus.RETURNED_FOR_CORRECTION),
        (ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.PENDING_CONFIRM),
        (ResultStatus.FAILED, ResultStatus.PENDING_CONFIRM),
    ],
)
async def test_retakeKeepsAReturnedResultReturnedAndResetsOtherStatuses(before: str, after: str) -> None:
    service, _ = _serviceReturning(_makeExistingResult(before))

    result = await service._getOrCreateResult(_makeSpecimen(), IMAGE_ID)

    assert result.status == after
    assert result.imageId == IMAGE_ID


@pytest.mark.asyncio
async def test_retakeClearsTheOldImagesFindingsAndSmartDiagnosis() -> None:
    service, db = _serviceReturning(_makeExistingResult(ResultStatus.PENDING_CONFIRM))

    result = await service._getOrCreateResult(_makeSpecimen(), IMAGE_ID)

    assert (result.aiFindings, result.flaggedAnomalies, result.particleClasses) == ({}, {}, {})
    assert result.smartDiagnosis is None
    assert result.aiDetections is None
    assert result.smartDiagnosisUnavailable is False
    assert result.modelVersion == config.settings.aiModelVersion
    [stmt] = [c.args[0] for c in db.execute.await_args_list if isinstance(c.args[0], Delete)]
    assert stmt.table.name == "smart_diagnosis_outputs"
    assert stmt.whereclause.right.value == RESULT_ID
    [clearBoxes] = [c.args[0] for c in db.execute.await_args_list if isinstance(c.args[0], Update)]
    assert clearBoxes.table.name == "result_reviews"
    assert clearBoxes.whereclause.right.value == RESULT_ID
    sql = str(clearBoxes.compile())
    assert "spatial_annotations=" in sql
    assert "annotation_notes=" not in sql


@pytest.mark.asyncio
async def test_aFirstUploadCreatesAResultWithDiagnosisAvailable() -> None:
    service, db = _serviceReturning(None)

    result = await service._getOrCreateResult(_makeSpecimen(), IMAGE_ID)

    assert result.status == ResultStatus.PENDING_CONFIRM
    assert result.smartDiagnosisUnavailable is False
    db.add.assert_called_once_with(result)


# ── AI analysis ───────────────────────────────────────────────────────────────

def _patchInfer(infer: object) -> object:
    import builtins

    realImport = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "urolens_ai":
            return MagicMock(infer=infer)
        return realImport(name, *args, **kwargs)

    return patch("builtins.__import__", _import)


@pytest.mark.asyncio
async def test_findingsAreNormalizedToUnderscores() -> None:
    infer = MagicMock(return_value=SimpleNamespace(
        particles={"epithelial-cells": 3, "erythrocytes": 0}, model_version="test-model",
    ))
    service, _ = _serviceReturning(None)

    with _patchInfer(infer):
        findings = await service._infer(SPECIMEN_ID, b"jpeg")

    assert findings.findings == {"epithelial_cells": 3, "erythrocytes": 0}
    assert findings.detections is None  # Older engine packages have only counts.


@pytest.mark.asyncio
async def test_nothingDetectedIsARealResultNotAFailure() -> None:
    service, _ = _serviceReturning(None)

    with _patchInfer(MagicMock(return_value=SimpleNamespace(
        particles={}, detections=[], model_version="test-model",
    ))):
        inference = await service._infer(SPECIMEN_ID, b"jpeg")
    assert inference.findings == {}
    assert inference.detections == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "infer",
    [MagicMock(side_effect=RuntimeError("model weights not found")), MagicMock(side_effect=MemoryError())],
)
async def test_aFailedAnalysisRaisesAiAnalysisFailed(infer: MagicMock) -> None:
    service, _ = _serviceReturning(None)

    with _patchInfer(infer), pytest.raises(AIAnalysisError) as excInfo:
        await service._infer(SPECIMEN_ID, b"jpeg")

    assert excInfo.value.status_code == 503
    assert excInfo.value.errorCode == "AI_ANALYSIS_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("NOT_MICROSCOPY", "This does not look like a urine microscopy image. Please retake."),
        ("IMAGE_EXPOSURE", "The image is too dark to analyse. Check the microscope light."),
    ],
)
async def test_aGateRejectedImageIsUnprocessableNotAiAnalysisFailed(code: str, message: str) -> None:
    from urolens_ai.utils.exceptions import ImageValidationError

    infer = MagicMock(side_effect=ImageValidationError(code=code, message=message))
    service, _ = _serviceReturning(None)

    with _patchInfer(infer), pytest.raises(UnprocessableException) as excInfo:
        await service._infer(SPECIMEN_ID, b"jpeg")

    assert excInfo.value.status_code == 422
    assert excInfo.value.errorCode == code
    assert excInfo.value.detail == message


@pytest.mark.asyncio
async def test_aMissingAiPackageIsAFailedAnalysis() -> None:
    import builtins

    realImport = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "urolens_ai":
            raise ImportError("No module named 'urolens_ai'")
        return realImport(name, *args, **kwargs)

    service, _ = _serviceReturning(None)
    with patch("builtins.__import__", _import), pytest.raises(AIAnalysisError):
        await service._infer(SPECIMEN_ID, b"jpeg")


# ── Storage ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aiBoxesAreConvertedToImagePercentagesAndRetainConfidence() -> None:
    output = SimpleNamespace(
        particles={"epithelial-cells": 1}, model_version="box-model",
        image_width=1000, image_height=500,
        detections=[SimpleNamespace(
            class_name="epithelial-cells", confidence=0.85, bbox=[200, 150, 300, 190],
        )],
    )
    service, _ = _serviceReturning(None)
    with _patchInfer(MagicMock(return_value=output)):
        inference = await service._infer(SPECIMEN_ID, b"jpeg")
    [box] = inference.detections
    assert box == {
        "id": box["id"], "particleType": "epithelial_cells", "confidence": 0.85,
        "x": 20, "y": 30, "w": 10, "h": 8,
    }
    assert uuid.UUID(box["id"])
    assert inference.modelVersion == "box-model"
    assert inference.findings == {"epithelial_cells": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("bbox", [[float("nan"), 0, 10, 10], [20, 20, 10, 10], [1000, 0, 1100, 20]])
async def test_invalidDetectionGeometryFailsAnalysisInsteadOfSavingMisleadingBoxes(bbox: list) -> None:
    output = SimpleNamespace(
        particles={"bacteria": 1}, model_version="box-model", image_width=1000, image_height=500,
        detections=[SimpleNamespace(class_name="bacteria", confidence=0.8, bbox=bbox)],
    )
    service, _ = _serviceReturning(None)
    with _patchInfer(MagicMock(return_value=output)), pytest.raises(AIAnalysisError):
        await service._infer(SPECIMEN_ID, b"jpeg")


@pytest.mark.asyncio
async def test_aStorageFailureRaisesStorageError() -> None:
    sb = MagicMock()
    sb.storage.from_.return_value.upload = AsyncMock(side_effect=RuntimeError("bucket unreachable"))
    service, _ = _serviceReturning(None)

    with patch.object(ai_integration_service, "sb", sb), pytest.raises(StorageError) as excInfo:
        await service._uploadToStorage(b"jpeg", "specimens/x/images/y.jpg", "image/jpeg")

    assert excInfo.value.status_code == 503
    assert excInfo.value.errorCode == "STORAGE_ERROR"


# ── Metadata is stripped on the server ────────────────────────────────────────

def _jpegWithExif() -> bytes:
    img = PILImage.new("RGB", (800, 600), (120, 180, 240))
    exif = PILImage.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0110] = "PhoneModel"  # Model
    exif.get_ifd(0x8825).update({1: "N", 2: (14.0, 35.0, 0.0)})  # GPS latitude
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, exif=exif)
    return buf.getvalue()


def _pngWithText() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Location", "14.58 N, 120.98 E")
    buf = io.BytesIO()
    PILImage.new("RGB", (800, 600), (10, 20, 30)).save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_jpegExifIncludingLocationIsRemoved() -> None:
    original = _jpegWithExif()
    assert PILImage.open(io.BytesIO(original)).getexif()  # the fixture really has EXIF
    service, _ = _serviceReturning(None)

    cleaned = await service._stripMetadata(original, "image/jpeg")

    stripped = PILImage.open(io.BytesIO(cleaned))
    assert stripped.format == "JPEG"
    assert stripped.size == (800, 600)
    assert not stripped.getexif()
    assert b"PhoneModel" not in cleaned and b"Exif" not in cleaned


@pytest.mark.asyncio
async def test_jpegQualityIsKept() -> None:
    original = _jpegWithExif()
    service, _ = _serviceReturning(None)

    cleaned = await service._stripMetadata(original, "image/jpeg")

    before = PILImage.open(io.BytesIO(original))
    after = PILImage.open(io.BytesIO(cleaned))
    assert after.quantization == before.quantization


@pytest.mark.asyncio
async def test_pngTextChunksAreRemoved() -> None:
    service, _ = _serviceReturning(None)

    cleaned = await service._stripMetadata(_pngWithText(), "image/png")

    stripped = PILImage.open(io.BytesIO(cleaned))
    assert stripped.format == "PNG"
    assert "Location" not in stripped.info
    assert b"14.58 N" not in cleaned


@pytest.mark.asyncio
async def test_anImageThatCantBeDecodedIsAnInvalidFormat() -> None:
    truncated = _jpegWithExif()[:400]
    service, _ = _serviceReturning(None)

    with pytest.raises(ImageFormatError):
        await service._stripMetadata(truncated, "image/jpeg")


@pytest.mark.asyncio
async def test_reencodingPastTheSizeCapIsRefused() -> None:
    service, _ = _serviceReturning(None)

    with patch.object(ai_integration_service, "MAX_IMAGE_BYTES", 100), pytest.raises(ImageTooLargeError):
        await service._stripMetadata(_pngWithText(), "image/png")


# ── The upload starts the specimen and stores the cleaned image ───────────────

async def _upload(specimen: Specimen, result: AnalysisResult) -> tuple[AsyncMock, MagicMock, AIIntegrationService]:
    db = AsyncMock()
    db.add = MagicMock()
    auditLogger = MagicMock(spec=AuditLogger, record=AsyncMock())
    service = AIIntegrationService(db=db, auditLogger=auditLogger)
    with patch.multiple(
        service,
        _readWithinLimit=AsyncMock(return_value=b"raw"),
        _requireUploadAllowed=AsyncMock(return_value=specimen),
        _validateImage=AsyncMock(return_value=(800, 600)),
        _stripMetadata=AsyncMock(return_value=b"cleaned"),
        _infer=AsyncMock(return_value=InferenceOutput(
            findings={"erythrocytes": 2}, detections=[{
                "id": "new-box", "particleType": "erythrocytes", "confidence": 0.9,
                "x": 10, "y": 10, "w": 5, "h": 5,
            }], modelVersion="new-model",
        )),
        _replacePreviousImage=AsyncMock(),
        _uploadToStorage=AsyncMock(),
        _getOrCreateResult=AsyncMock(return_value=result),
        _clearManualOverrides=AsyncMock(return_value=0),
        _runSmartDiagnosis=AsyncMock(),
    ):
        await service.handleUpload(SPECIMEN_ID, MEDTECH_ID, MagicMock(content_type="image/jpeg"), None)
        service._uploadToStorage.assert_awaited_once()
        storedBytes = service._uploadToStorage.await_args.args[0]
        inferredBytes = service._infer.await_args.args[1]
    assert storedBytes == inferredBytes == b"cleaned"
    return db, auditLogger, service


@pytest.mark.asyncio
@pytest.mark.parametrize("startStatus", ["ASSIGNED", "IN_QUEUE"])
async def test_anUploadStartsASpecimenThatWasNotStartedYet(startStatus: str) -> None:
    specimen = _makeSpecimen(status=startStatus)

    db, auditLogger, _ = await _upload(specimen, _makeExistingResult(ResultStatus.PENDING_CONFIRM))

    assert specimen.status == "PROCESSING"
    events = [c.kwargs["eventType"] for c in auditLogger.record.await_args_list]
    assert "IMAGE_UPLOADED" in events
    uploaded = next(c.kwargs for c in auditLogger.record.await_args_list if c.kwargs["eventType"] == "IMAGE_UPLOADED")
    assert uploaded["detailJson"]["started_analysis"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_anUploadLeavesAProcessingSpecimenAsItIs() -> None:
    specimen = _makeSpecimen(status="PROCESSING")

    _, auditLogger, _ = await _upload(specimen, _makeExistingResult(ResultStatus.PENDING_CONFIRM))

    assert specimen.status == "PROCESSING"
    uploaded = auditLogger.record.await_args.kwargs
    assert uploaded["eventType"] == "IMAGE_UPLOADED"
    assert uploaded["detailJson"]["started_analysis"] is False


@pytest.mark.asyncio
async def test_theResultGetsTheNewFindingsAndItsAnomalies() -> None:
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)

    await _upload(_makeSpecimen(), result)

    assert result.aiFindings == {"erythrocytes": 2}
    assert result.flaggedAnomalies == {"erythrocytes": 2}
    assert result.aiDetections[0]["id"] == "new-box"
    assert result.modelVersion == "new-model"


# ── Smart Diagnosis: one row per result ───────────────────────────────────────

def _engineOutput() -> MagicMock:
    output = MagicMock()
    for condition, level in (("gout", "HIGH"), ("glomerulonephritis", "LOW"), ("nephrolithiasis", "LOW")):
        getattr(output, condition).level.value = level
        getattr(output, condition).weighted_score = 1.0
        getattr(output, condition).evidence = []
    output.no_significant_indicators = False
    output.engine_version = "mvp-v1.0"
    return output


def _sessionWithSavepoints() -> AsyncMock:
    # `begin_nested()` is a sync call returning an async context manager.
    db = AsyncMock()
    db.add = MagicMock()
    savepoint = AsyncMock()
    savepoint.__aenter__ = AsyncMock(return_value=savepoint)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=savepoint)
    return db


def _smartDiagnosisService() -> SmartDiagnosisService:
    notif = MagicMock(spec=NotificationService)
    notif.notifySupervisorDiagnosisUnavailable = AsyncMock()
    return SmartDiagnosisService(auditLogger=MagicMock(record=AsyncMock()), _notifService=notif)


@pytest.mark.asyncio
async def test_reconfirmingUpdatesTheExistingDiagnosisInsteadOfInsertingASecond() -> None:
    existing = SmartDiagnosisOutput(resultId=RESULT_ID, goutScore="LOW", gnScore="LOW", nephroScore="LOW",
                                    noSignificantIndicators=True, evidenceMap={}, engineVersion="old",
                                    status="FLAGGED_UNAVAILABLE")
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)
    result.smartDiagnosisOutput = existing
    db = _sessionWithSavepoints()

    with patch.object(SmartDiagnosisService, "_loadResult", AsyncMock(return_value=result)), \
         patch("urolens_ai.generate_smart_diagnosis", return_value=_engineOutput()):
        output = await _smartDiagnosisService().run(resultId=RESULT_ID, db=db)

    assert output is existing
    db.add.assert_not_called()
    assert (existing.goutScore, existing.status, existing.engineVersion) == ("HIGH", "ATTACHED", "mvp-v1.0")
    assert existing.noSignificantIndicators is False
    assert result.smartDiagnosisUnavailable is False
    assert result.smartDiagnosis["gout"] is not None


@pytest.mark.asyncio
async def test_aFailedDiagnosisFlagsTheEarlierOneUnavailable() -> None:
    previous = MagicMock(spec=SmartDiagnosisOutput)
    previous.status = "ATTACHED"
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)

    def _execute(stmt: object) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        return MagicMock(scalar_one_or_none=MagicMock(return_value=previous if entity is SmartDiagnosisOutput else result))

    db = _sessionWithSavepoints()
    db.execute = AsyncMock(side_effect=_execute)

    with patch.object(SmartDiagnosisService, "_loadResult", AsyncMock(side_effect=RuntimeError("engine down"))):
        output = await _smartDiagnosisService().run(resultId=RESULT_ID, db=db)

    assert output is None
    assert previous.status == "FLAGGED_UNAVAILABLE"
    assert result.smartDiagnosisUnavailable is True


# ── A discarded image's result can't be confirmed ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("imageStatus", "refused"),
    [(ImageStatus.DISCARDED, True), (ImageStatus.REPLACED, True), (ImageStatus.ACTIVE, False)],
)
async def test_retakeGuardRefusesAResultWhoseImageIsNotActive(imageStatus: str, refused: bool) -> None:
    image = MagicMock(spec=Image)
    image.status = imageStatus
    db = AsyncMock()
    db.get = AsyncMock(return_value=image)
    service = ResultConfirmationService(
        db=db, auditLogger=MagicMock(), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)
    result.imageId = IMAGE_ID

    if refused:
        with pytest.raises(UnprocessableException) as excInfo:
            await service._validateNoPendingRetake(result)
        assert excInfo.value.errorCode == "PENDING_RETAKE"
    else:
        await service._validateNoPendingRetake(result)
    db.get.assert_awaited_once_with(Image, IMAGE_ID)


@pytest.mark.asyncio
async def test_aResultWithNoImageIsNotLookedUp() -> None:
    db = AsyncMock()
    service = ResultConfirmationService(
        db=db, auditLogger=MagicMock(), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)
    result.imageId = None

    await service._validateNoPendingRetake(result)

    db.get.assert_not_awaited()


# ── Model weights default ─────────────────────────────────────────────────────

def test_modelWeightsDefaultToThoseBundledInTheAiPackage() -> None:
    with tempfile.TemporaryDirectory() as packageDir:
        weights = Path(packageDir) / "models" / "yolov8" / "best.pt"
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"weights")
        spec = MagicMock(submodule_search_locations=[packageDir])

        with patch.object(config.importlib.util, "find_spec", return_value=spec):
            assert config._bundledModelWeightsPath() == str(weights)


@pytest.mark.parametrize("spec", [None, MagicMock(submodule_search_locations=["/nowhere"])])
def test_noBundledWeightsGivesAnEmptyPath(spec: object) -> None:
    with patch.object(config.importlib.util, "find_spec", return_value=spec):
        assert config._bundledModelWeightsPath() == ""


def test_theEngineIsToldWhereTheBundledWeightsAre() -> None:
    env = {k: v for k, v in os.environ.items() if k != "MODEL_WEIGHTS_PATH"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(config, "_bundledModelWeightsPath", return_value="/engine/best.pt"):
        loaded = config._loadSettings()
        assert os.environ["MODEL_WEIGHTS_PATH"] == "/engine/best.pt"
    assert loaded.modelWeightsPath == "/engine/best.pt"


def test_anExplicitWeightsPathWins() -> None:
    with patch.dict(os.environ, {"MODEL_WEIGHTS_PATH": "/custom/model.pt"}), \
         patch.object(config, "_bundledModelWeightsPath", return_value="/engine/best.pt"):
        assert config._loadSettings().modelWeightsPath == "/custom/model.pt"


# ── Gate weights default ──────────────────────────────────────────────────────

def test_gateWeightsDefaultToThoseBundledInTheAiPackage() -> None:
    with tempfile.TemporaryDirectory() as packageDir:
        weights = Path(packageDir) / "models" / "gate" / "gate.pt"
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"weights")
        spec = MagicMock(submodule_search_locations=[packageDir])

        with patch.object(config.importlib.util, "find_spec", return_value=spec):
            assert config._bundledGateWeightsPath() == str(weights)


@pytest.mark.parametrize("spec", [None, MagicMock(submodule_search_locations=["/nowhere"])])
def test_noBundledGateWeightsGivesAnEmptyPath(spec: object) -> None:
    with patch.object(config.importlib.util, "find_spec", return_value=spec):
        assert config._bundledGateWeightsPath() == ""


def test_theEngineIsToldWhereTheBundledGateWeightsAre() -> None:
    env = {k: v for k, v in os.environ.items() if k != "GATE_WEIGHTS_PATH"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(config, "_bundledGateWeightsPath", return_value="/engine/gate.pt"):
        loaded = config._loadSettings()
        assert os.environ["GATE_WEIGHTS_PATH"] == "/engine/gate.pt"
    assert loaded.gateWeightsPath == "/engine/gate.pt"


def test_anExplicitGateWeightsPathWins() -> None:
    with patch.dict(os.environ, {"GATE_WEIGHTS_PATH": "/custom/gate.pt"}), \
         patch.object(config, "_bundledGateWeightsPath", return_value="/engine/gate.pt"):
        assert config._loadSettings().gateWeightsPath == "/custom/gate.pt"


# ── Smart Diagnosis failing at upload is reported ─────────────────────────────

@pytest.mark.asyncio
async def test_aDiagnosisFailureAtUploadMarksItUnavailable() -> None:
    import builtins

    realImport = builtins.__import__

    def _import(name: str, *args: object, **kwargs: object) -> object:
        if name == "urolens_ai":
            return MagicMock(generate_smart_diagnosis=MagicMock(side_effect=RuntimeError("engine down")))
        return realImport(name, *args, **kwargs)

    service, _ = _serviceReturning(None)
    result = _makeExistingResult(ResultStatus.PENDING_CONFIRM)
    result.smartDiagnosisUnavailable = False

    with patch("builtins.__import__", _import):
        assert await service._runSmartDiagnosis(result, {"erythrocytes": 2}) is None

    assert result.smartDiagnosisUnavailable is True
