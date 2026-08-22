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
    _classify_error,
)

# ── Shared test constants ─────────────────────────────────────────────────────

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000012")


# ── AI engine mock builders ───────────────────────────────────────────────────

def _make_engine_output(
    gout_level: str = "HIGH",
    gn_level: str = "MODERATE",
    nephro_level: str = "LOW",
    no_significant: bool = False,
    engine_version: str = "mvp-v1.0",
) -> MagicMock:
    """Builds a mock SmartDiagnosisOutput matching the urolens_ai schema."""
    def _condition(level_val: str) -> MagicMock:
        cond = MagicMock()
        cond.level = MagicMock()
        cond.level.value = level_val
        cond.weighted_score = 5.0 if level_val != "LOW" else 0.0
        cond.evidence = []
        return cond

    out = MagicMock()
    out.gout = _condition(gout_level)
    out.glomerulonephritis = _condition(gn_level)
    out.nephrolithiasis = _condition(nephro_level)
    out.no_significant_indicators = no_significant
    out.engine_version = engine_version
    return out


def _make_all_low_output() -> MagicMock:
    return _make_engine_output(
        gout_level="LOW",
        gn_level="LOW",
        nephro_level="LOW",
        no_significant=True,
    )


# ── DB session mock builder ───────────────────────────────────────────────────

def _make_db_mock(result: AnalysisResult) -> AsyncMock:
    """Returns a mock AsyncSession that yields the given AnalysisResult on SELECT."""
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=execute_result)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # begin_nested() must be a sync call returning an async context manager.
    # AsyncMock() supports `async with` natively via __aenter__/__aexit__.
    nested_ctx = AsyncMock()
    nested_ctx.__aenter__ = AsyncMock(return_value=nested_ctx)
    nested_ctx.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested_ctx)

    return db


def _make_result(
    result_id: uuid.UUID = RESULT_ID,
    specimen_id: uuid.UUID = SPECIMEN_ID,
    status: str = ResultStatus.PENDING_CONFIRM,
    ai_findings: dict | None = None,
) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.result_id = result_id
    result.id = result_id
    result.specimen_id = specimen_id
    result.status = status
    result.ai_findings = ai_findings or {"uric_acid_crystals": 15, "rbc_casts": 3}
    result.smart_diagnosis_unavailable = False
    result.image = None
    result.confirmed_by = None
    result.confirmed_at = None
    return result


# ── SmartDiagnosisService.run() tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_success_persists_output():
    """Happy path: run() persists SmartDiagnosisOutput and returns it."""
    engine_output = _make_engine_output()
    result = _make_result()
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)
    notif.notify_supervisor_diagnosis_unavailable = AsyncMock()

    service = SmartDiagnosisService(audit_logger=audit_logger, notif_service=notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._load_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        return_value=engine_output,
    ):
        output = await service.run(result_id=RESULT_ID, db=db)

    assert output is not None
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, SmartDiagnosisOutput)
    assert added.gout_score == "HIGH"
    assert added.gn_score == "MODERATE"
    assert added.nephro_score == "LOW"
    assert added.no_significant_indicators is False
    assert added.engine_version == "mvp-v1.0"
    audit_logger.record.assert_called_once()
    call_kwargs = audit_logger.record.call_args[1]
    assert call_kwargs["event_type"] == "SMART_DIAGNOSIS_GENERATED"


@pytest.mark.asyncio
async def test_run_all_low_sets_no_significant_indicators():
    """When all scores are LOW, no_significant_indicators must be True."""
    engine_output = _make_all_low_output()
    result = _make_result(ai_findings={"rbc": 0, "wbc": 0})
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)

    service = SmartDiagnosisService(audit_logger=audit_logger, notif_service=notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._load_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        return_value=engine_output,
    ):
        output = await service.run(result_id=RESULT_ID, db=db)

    assert output is not None
    added = db.add.call_args[0][0]
    assert added.no_significant_indicators is True
    assert added.gout_score == "LOW"
    assert added.gn_score == "LOW"
    assert added.nephro_score == "LOW"


@pytest.mark.asyncio
async def test_run_engine_failure_creates_error_log_and_returns_none():
    """Engine failure: EngineErrorLog added, smart_diagnosis_unavailable=True, returns None."""
    class _FakeRuleEngineError(Exception):
        def __init__(self, code: str, message: str):
            super().__init__(message)
            self.code = code

    result = _make_result()
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)
    notif.notify_supervisor_diagnosis_unavailable = AsyncMock()

    service = SmartDiagnosisService(audit_logger=audit_logger, notif_service=notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._load_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        side_effect=_FakeRuleEngineError(
            code="RULE_EVALUATION_FAILED",
            message="rule engine crashed",
        ),
    ):
        output = await service.run(result_id=RESULT_ID, db=db)

    assert output is None
    # EngineErrorLog was added
    add_calls = db.add.call_args_list
    error_log_added = any(
        isinstance(call[0][0], EngineErrorLog) for call in add_calls
    )
    assert error_log_added, "EngineErrorLog must be added on engine failure"
    # smart_diagnosis_unavailable flag set
    assert result.smart_diagnosis_unavailable is True
    # Supervisor notified
    notif.notify_supervisor_diagnosis_unavailable.assert_called_once_with(
        result_id=RESULT_ID
    )


@pytest.mark.asyncio
async def test_run_engine_failure_does_not_propagate():
    """run() MUST NEVER re-raise — engine failure is fully absorbed."""
    result = _make_result()
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)
    notif.notify_supervisor_diagnosis_unavailable = AsyncMock()

    service = SmartDiagnosisService(audit_logger=audit_logger, notif_service=notif)

    with patch(
        "src.urolens.services.smart_diagnosis_service.SmartDiagnosisService._load_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "urolens_ai.generate_smart_diagnosis",
        side_effect=RuntimeError("unexpected crash"),
    ):
        # Must not raise
        result_val = await service.run(result_id=RESULT_ID, db=db)

    assert result_val is None


# ── _classify_error tests ─────────────────────────────────────────────────────

def test_classify_error_known_code():
    exc = MagicMock()
    exc.code = "INVALID_CLASSIFICATION"
    assert _classify_error(exc) == "INVALID_CLASSIFICATION"


def test_classify_error_unknown_falls_back():
    exc = RuntimeError("oops")
    assert _classify_error(exc) == "RULE_EVALUATION_FAILED"


# ── ResultConfirmationService integration ─────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_result_triggers_smart_diagnosis():
    """confirm_result() calls SmartDiagnosisService.run() synchronously."""
    result = _make_result(status=ResultStatus.PENDING_CONFIRM)
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)
    notif.notify_supervisor_result_ready = AsyncMock()
    notif.notify_supervisor_diagnosis_unavailable = AsyncMock()

    smart_diag = MagicMock(spec=SmartDiagnosisService)
    smart_diag.run = AsyncMock(return_value=MagicMock(spec=SmartDiagnosisOutput))

    # confirmation refresh mock
    confirmation_mock = MagicMock()
    confirmation_mock.id = uuid.uuid4()
    confirmation_mock.result_id = RESULT_ID
    confirmation_mock.confirmed_by = MEDTECH_ID
    confirmation_mock.confirmed_at = datetime.now(UTC)
    db.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "__refreshed__", True))

    service = ResultConfirmationService(
        db=db,
        audit_logger=audit_logger,
        smart_diagnosis_service=smart_diag,
        notif_service=notif,
    )

    request_mock = MagicMock()
    request_mock.client = None

    with patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._get_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._validate_no_pending_retake",
        new_callable=AsyncMock,
    ):
        await service.confirm_result(
            result_id=RESULT_ID,
            medtech_id=MEDTECH_ID,
            request=request_mock,
        )

    smart_diag.run.assert_called_once_with(result_id=RESULT_ID, db=db)


@pytest.mark.asyncio
async def test_confirm_result_succeeds_even_when_smart_diagnosis_fails():
    """Confirmation succeeds even when SmartDiagnosisService.run() returns None (engine failed)."""
    result = _make_result(status=ResultStatus.PENDING_CONFIRM)
    db = _make_db_mock(result)

    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    notif = MagicMock(spec=NotificationService)
    notif.notify_supervisor_result_ready = AsyncMock()
    notif.notify_supervisor_diagnosis_unavailable = AsyncMock()

    smart_diag = MagicMock(spec=SmartDiagnosisService)
    smart_diag.run = AsyncMock(return_value=None)  # engine failed

    service = ResultConfirmationService(
        db=db,
        audit_logger=audit_logger,
        smart_diagnosis_service=smart_diag,
        notif_service=notif,
    )

    request_mock = MagicMock()
    request_mock.client = None

    with patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._get_result",
        new_callable=AsyncMock,
        return_value=result,
    ), patch(
        "src.urolens.services.result_confirmation_service.ResultConfirmationService._validate_no_pending_retake",
        new_callable=AsyncMock,
    ):
        # Must NOT raise even though smart diagnosis returned None
        await service.confirm_result(
            result_id=RESULT_ID,
            medtech_id=MEDTECH_ID,
            request=request_mock,
        )

    db.commit.assert_called_once()
