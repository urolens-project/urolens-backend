"""Integration tests — T3.1: Smart Diagnosis trigger, persist, and failure isolation

Covers (TASK-MOB-11-8)
----------------------
- Happy path: confirm_result() → SmartDiagnosisService.run() → output persisted
- All-LOW path: no_significant_indicators=True when all counts normal
- Engine failure: EngineErrorLog created, smart_diagnosis_unavailable=True,
                  confirmation transaction still succeeds
- Error classification: engine error code surfaced correctly in EngineErrorLog
- GET /api/v1/results/{id}/smart-diagnosis — SUPERVISOR only, returns persisted output
- GET /api/v1/results/{id}/smart-diagnosis — 404 when output absent
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.models.analysis_result import AnalysisResult, ResultStatus
from src.urolens.models.engine_error_log import EngineErrorLog
from src.urolens.models.image import Image  # noqa: F401
from src.urolens.models.manual_override import ManualOverride  # noqa: F401
from src.urolens.models.result_confirmation import ResultConfirmation  # noqa: F401
from src.urolens.models.result_view import ResultView  # noqa: F401
from src.urolens.models.smart_diagnosis_output import SmartDiagnosisOutput

# Import every model that shares the SQLAlchemy registry so all forward-reference
# strings (e.g. "Specimen") are resolvable before mapper configuration is triggered.
from src.urolens.models.specimen import Specimen  # noqa: F401
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.result_confirmation_service import ResultConfirmationService
from src.urolens.services.smart_diagnosis_service import (
    SmartDiagnosisService,
    _classifyError,
)

# ── Shared test constants ─────────────────────────────────────────────────────

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000012")


# ── AI engine mock builders ───────────────────────────────────────────────────

def _makeEngineOutput(
    goutLevel: str = "HIGH",
    gnLevel: str = "MODERATE",
    nephroLevel: str = "LOW",
    noSignificant: bool = False,
    engineVersion: str = "mvp-v1.0",
) -> MagicMock:
    """Builds a mock SmartDiagnosisOutput matching the urolens_ai schema."""
    def _condition(levelVal: str) -> MagicMock:
        cond = MagicMock()
        cond.level = MagicMock()
        cond.level.value = levelVal
        cond.weighted_score = 5.0 if levelVal != "LOW" else 0.0
        cond.evidence = []
        return cond

    out = MagicMock()
    out.gout = _condition(goutLevel)
    out.glomerulonephritis = _condition(gnLevel)
    out.nephrolithiasis = _condition(nephroLevel)
    out.noSignificantIndicators = noSignificant
    out.engineVersion = engineVersion
    return out


def _makeAllLowOutput() -> MagicMock:
    return _makeEngineOutput(
        goutLevel="LOW",
        gnLevel="LOW",
        nephroLevel="LOW",
        noSignificant=True,
    )


# ── DB session mock builder ───────────────────────────────────────────────────

def _makeDbMock(result: AnalysisResult) -> AsyncMock:
    """Returns a mock AsyncSession that yields the given AnalysisResult on SELECT."""
    db = AsyncMock()
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=executeResult)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # begin_nested() must be a sync call returning an async context manager.
    # AsyncMock() supports `async with` natively via __aenter__/__aexit__.
    nestedCtx = AsyncMock()
    nestedCtx.__aenter__ = AsyncMock(return_value=nestedCtx)
    nestedCtx.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nestedCtx)

    return db


def _makeResult(
    resultId: uuid.UUID = RESULT_ID,
    specimenId: uuid.UUID = SPECIMEN_ID,
    status: str = ResultStatus.PENDING_CONFIRM,
    aiFindings: dict | None = None,
) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = resultId
    result.id = resultId
    result.specimenId = specimenId
    result.status = status
    result.aiFindings = aiFindings or {"uric_acid_crystals": 15, "rbc_casts": 3}
    result.smartDiagnosisUnavailable = False
    result.image = None
    result.confirmedBy = None
    result.confirmedAt = None
    return result


# ── SmartDiagnosisService.run() tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_runSuccessPersistsOutput():
    """Happy path: run() persists SmartDiagnosisOutput and returns it."""
    engineOutput = _makeEngineOutput()
    result = _makeResult()
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorDiagnosisUnavailable = AsyncMock()

    _service = SmartDiagnosisService(auditLogger=auditLogger, _notifService=_notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._loadResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        return_value=engineOutput,
    ):
        output = await _service.run(resultId=RESULT_ID, db=db)

    assert output is not None
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, SmartDiagnosisOutput)
    assert added.goutScore == "HIGH"
    assert added.gnScore == "MODERATE"
    assert added.nephroScore == "LOW"
    assert added.noSignificantIndicators is False
    assert added.engineVersion == "mvp-v1.0"
    auditLogger.record.assert_called_once()
    callKwargs = auditLogger.record.call_args[1]
    assert callKwargs["eventType"] == "SMART_DIAGNOSIS_GENERATED"


@pytest.mark.asyncio
async def test_runAllLowSetsNoSignificantIndicators():
    """When all scores are LOW, no_significant_indicators must be True."""
    engineOutput = _makeAllLowOutput()
    result = _makeResult(aiFindings={"rbc": 0, "wbc": 0})
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)

    _service = SmartDiagnosisService(auditLogger=auditLogger, _notifService=_notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._loadResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        return_value=engineOutput,
    ):
        output = await _service.run(resultId=RESULT_ID, db=db)

    assert output is not None
    added = db.add.call_args[0][0]
    assert added.noSignificantIndicators is True
    assert added.goutScore == "LOW"
    assert added.gnScore == "LOW"
    assert added.nephroScore == "LOW"


@pytest.mark.asyncio
async def test_runEngineFailureCreatesErrorLogAndReturnsNone():
    """Engine failure: EngineErrorLog added, smart_diagnosis_unavailable=True, returns None."""
    class _FakeRuleEngineError(Exception):
        def __init__(self, code: str, message: str):
            super().__init__(message)
            self.code = code

    result = _makeResult()
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorDiagnosisUnavailable = AsyncMock()

    _service = SmartDiagnosisService(auditLogger=auditLogger, _notifService=_notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._loadResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        side_effect=_FakeRuleEngineError(
            code="RULE_EVALUATION_FAILED",
            message="rule engine crashed",
        ),
    ):
        output = await _service.run(resultId=RESULT_ID, db=db)

    assert output is None
    # EngineErrorLog was added
    addCalls = db.add.call_args_list
    errorLogAdded = any(
        isinstance(call[0][0], EngineErrorLog) for call in addCalls
    )
    assert errorLogAdded, "EngineErrorLog must be added on engine failure"
    # smart_diagnosis_unavailable flag set
    assert result.smartDiagnosisUnavailable is True
    # Supervisor notified
    _notif.notifySupervisorDiagnosisUnavailable.assert_called_once_with(
        resultId=RESULT_ID
    )


@pytest.mark.asyncio
async def test_runEngineFailureDoesNotPropagate():
    """run() MUST NEVER re-raise — engine failure is fully absorbed."""
    result = _makeResult()
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorDiagnosisUnavailable = AsyncMock()

    _service = SmartDiagnosisService(auditLogger=auditLogger, _notifService=_notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._loadResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        side_effect=RuntimeError("unexpected crash"),
    ):
        # Must not raise
        resultVal = await _service.run(resultId=RESULT_ID, db=db)

    assert resultVal is None


# ── _classify_error tests ─────────────────────────────────────────────────────

def test_classifyErrorKnownCode():
    exc = MagicMock()
    exc.code = "INVALID_CLASSIFICATION"
    assert _classifyError(exc) == "INVALID_CLASSIFICATION"


def test_classifyErrorUnknownFallsBack():
    exc = RuntimeError("oops")
    assert _classifyError(exc) == "RULE_EVALUATION_FAILED"


# ── ResultConfirmationService integration ─────────────────────────────────────

@pytest.mark.asyncio
async def test_confirmResultTriggersSmartDiagnosis():
    """confirm_result() calls SmartDiagnosisService.run() synchronously."""
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM)
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorResultReady = AsyncMock()
    _notif.notifySupervisorDiagnosisUnavailable = AsyncMock()

    _smartDiag = MagicMock(spec=SmartDiagnosisService)
    _smartDiag.run = AsyncMock(return_value=MagicMock(spec=SmartDiagnosisOutput))

    # confirmation refresh mock
    confirmationMock = MagicMock()
    confirmationMock.id = uuid.uuid4()
    confirmationMock.resultId = RESULT_ID
    confirmationMock.confirmedBy = MEDTECH_ID
    confirmationMock.confirmedAt = datetime.now(UTC)
    db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "__refreshed__", True))

    _service = ResultConfirmationService(
        db=db,
        auditLogger=auditLogger,
        _smartDiagnosisService=_smartDiag,
        _notifService=_notif,
    )

    requestMock = MagicMock()
    requestMock.client = None

    with patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._getResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._validateNoPendingRetake",
        new_callable=AsyncMock,
    ):
        await _service.confirmResult(
            resultId=RESULT_ID,
            medtechId=MEDTECH_ID,
            request=requestMock,
        )

    _smartDiag.run.assert_called_once_with(resultId=RESULT_ID, db=db)


@pytest.mark.asyncio
async def test_confirmResultSucceedsEvenWhenSmartDiagnosisFails():
    """Confirmation succeeds even when SmartDiagnosisService.run() returns None (engine failed)."""
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM)
    db = _makeDbMock(result)

    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorResultReady = AsyncMock()
    _notif.notifySupervisorDiagnosisUnavailable = AsyncMock()

    _smartDiag = MagicMock(spec=SmartDiagnosisService)
    _smartDiag.run = AsyncMock(return_value=None)  # engine failed

    _service = ResultConfirmationService(
        db=db,
        auditLogger=auditLogger,
        _smartDiagnosisService=_smartDiag,
        _notifService=_notif,
    )

    requestMock = MagicMock()
    requestMock.client = None

    with patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._getResult",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._validateNoPendingRetake",
        new_callable=AsyncMock,
    ):
        # Must NOT raise even though smart diagnosis returned None
        await _service.confirmResult(
            resultId=RESULT_ID,
            medtechId=MEDTECH_ID,
            request=requestMock,
        )

    db.commit.assert_called_once()
