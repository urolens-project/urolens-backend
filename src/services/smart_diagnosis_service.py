"""Runs the AI Engineer's rule engine (Smart Diagnosis) against a confirmed
result's findings and persists the output — or, on any failure, logs the
error and marks the result diagnosis-unavailable without propagating (T3.1).
"""
import logging
import traceback
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.audit_logger import AuditLogger
from ..models.analysis_result import AnalysisResult
from ..models.engine_error_log import EngineErrorLog
from ..models.smart_diagnosis_output import SmartDiagnosisOutput
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

# Error codes from the AI engine contract
_RULE_ENGINE_ERROR_CODES = {
    "INVALID_CLASSIFICATION",
    "RULE_EVALUATION_FAILED",
    "CONFIG_ERROR",
}


class SmartDiagnosisService:
    """Triggers the AI Engineer's rule engine and persists the output (T3.1).

    Critical contract: run() MUST NEVER re-raise exceptions. Engine failure
    must not break the MedTech's confirmation flow. All exceptions are caught,
    logged to engine_error_logs, and the result is marked unavailable.
    """

    def __init__(
        self,
        auditLogger: AuditLogger,
        _notifService: NotificationService,
    ) -> None:
        self.auditLogger = auditLogger
        self._notifService = _notifService

    async def run(
        self,
        resultId: uuid.UUID,
        db: AsyncSession,
    ) -> SmartDiagnosisOutput | None:
        """Runs Smart Diagnosis for the given result.

        Runs inside a SAVEPOINT nested in the caller's transaction (`db`), so
        an engine failure rolls back only the diagnosis work, not the
        caller's parent transaction.

        Returns:
            The persisted `SmartDiagnosisOutput` on success; `None` on any
            failure (already logged to `engine_error_logs` and audited via
            `_handle_failure`).

        Raises:
            Never raises — all exceptions are handled internally, per the
            class's critical contract.
        """
        try:
            # SAVEPOINT isolates SmartDiagnosis from the parent confirmation
            # transaction — any SQL failure here rolls back to the savepoint
            # without aborting the parent transaction.
            async with db.begin_nested():
                result = await self._loadResult(resultId, db)
                classification: dict = result.aiFindings or {}

                # Call the AI Engineer's function — owned by urolens_ai package
                from urolens_ai import generate_smart_diagnosis  # type: ignore[import]
                engineOutput = generate_smart_diagnosis(classification)

                dbRecord = await self._persistOutput(engineOutput, resultId, db)

                await self.auditLogger.record(
                    eventType="SMART_DIAGNOSIS_GENERATED",
                    entityType="analysis_result",
                    entityId=resultId,
                    userId=None,
                    detailJson={
                        "gout_score":   engineOutput.gout.level.value,
                        "gn_score":     engineOutput.glomerulonephritis.level.value,
                        "nephro_score": engineOutput.nephrolithiasis.level.value,
                        "no_significant_indicators": engineOutput.no_significant_indicators,
                    },
                    db=db,
                    request=None,
                )
            return dbRecord

        except Exception as exc:
            logger.exception(
                "Smart Diagnosis engine failed for result_id=%s: %s", resultId, exc
            )
            await self._handleFailure(resultId, exc, db)
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _loadResult(
        self, resultId: uuid.UUID, db: AsyncSession
    ) -> AnalysisResult:
        # Loads the AnalysisResult for `run()`; raises ValueError if it doesn't exist.
        # Eagerly load smart_diagnosis_output so the relationship is in a
        # "loaded" state (None for fresh results) before db.add(SmartDiagnosisOutput).
        # Without this, the autoflush that INSERTs the new SmartDiagnosisOutput
        # triggers a back-reference update on AnalysisResult.smart_diagnosis_output.
        # In async SQLAlchemy, accessing an unloaded relationship during flush
        # raises MissingGreenlet, which is caught as engine failure.
        stmt = (
            select(AnalysisResult)
            .options(selectinload(AnalysisResult.smartDiagnosisOutput))
            .where(AnalysisResult.resultId == resultId)
        )
        row = await db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise ValueError(f"AnalysisResult {resultId} not found")
        return result

    async def _persistOutput(
        self, engineOutput, resultId: uuid.UUID, db: AsyncSession
    ) -> SmartDiagnosisOutput:
        # Persists a SmartDiagnosisOutput row and denormalizes a summary onto
        # analysis_results.smart_diagnosis for the mobile sync path to read.
        evidenceMap = _buildEvidenceMap(engineOutput)
        record = SmartDiagnosisOutput(
            resultId=resultId,
            goutScore=engineOutput.gout.level.value,
            gnScore=engineOutput.glomerulonephritis.level.value,
            nephroScore=engineOutput.nephrolithiasis.level.value,
            noSignificantIndicators=engineOutput.no_significant_indicators,
            evidenceMap=evidenceMap,
            engineVersion=engineOutput.engine_version,
            status="ATTACHED",
        )
        db.add(record)

        # Denormalize into analysis_results.smart_diagnosis so that the
        # mobile sync (which queries analysis_results directly) can read it.
        stmt = select(AnalysisResult).where(AnalysisResult.resultId == resultId)
        row = await db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is not None:
            result.smartDiagnosis = {
                "gout":               evidenceMap["gout"],
                "glomerulonephritis": evidenceMap["glomerulonephritis"],
                "nephrolithiasis":    evidenceMap["nephrolithiasis"],
                "no_significant_indicators": engineOutput.no_significant_indicators,
            }

        return record

    async def _handleFailure(
        self, resultId: uuid.UUID, exc: Exception, db: AsyncSession
    ) -> None:
        """Persists the engine error and marks the result as diagnosis-unavailable.

        Best-effort like the rest of the failure path: an exception here
        (e.g. the nested savepoint itself failing) is caught and logged, not
        propagated — `run()` must still return `None` cleanly.
        """
        try:
            errorCode = _classifyError(exc)
            async with db.begin_nested():
                errorLog = EngineErrorLog(
                    resultId=resultId,
                    errorCode=errorCode,
                    errorMessage=str(exc),
                    stackTrace=traceback.format_exc(),
                )
                db.add(errorLog)

                stmt = select(AnalysisResult).where(AnalysisResult.resultId == resultId)
                row = await db.execute(stmt)
                result = row.scalar_one_or_none()
                if result:
                    result.smartDiagnosisUnavailable = True

                await self.auditLogger.record(
                    eventType="ENGINE_FAILED",
                    entityType="analysis_result",
                    entityId=resultId,
                    userId=None,
                    detailJson={"error_code": errorCode, "error": str(exc)},
                    db=db,
                    request=None,
                )

            await self._notifService.notifySupervisorDiagnosisUnavailable(
                resultId=resultId,
            )
        except Exception:
            logger.exception(
                "Failed to persist engine error for result_id=%s", resultId
            )


def _classifyError(exc: Exception) -> str:
    """Maps an exception to the known engine error codes."""
    code = getattr(exc, "code", None)
    if code in _RULE_ENGINE_ERROR_CODES:
        return code
    return "RULE_EVALUATION_FAILED"


def _buildEvidenceMap(engineOutput) -> dict:
    """Serialises the per-condition evidence lists to a plain dict for JSONB storage."""
    def _serialiseCondition(condScore) -> dict:
        return {
            "level": condScore.level.value,
            "weighted_score": condScore.weighted_score,
            "evidence": [
                {
                    "particle_name": e.particle_name,
                    "particle_display_name": e.particle_display_name,
                    "detected_count": e.detected_count,
                    "normal_range_max": e.normal_range_max,
                    "contribution_weight": e.contribution_weight,
                    "contribution_score": e.contribution_score,
                    "contribution_role": e.contribution_role,
                }
                for e in condScore.evidence
            ],
        }

    return {
        "gout":               _serialiseCondition(engineOutput.gout),
        "glomerulonephritis": _serialiseCondition(engineOutput.glomerulonephritis),
        "nephrolithiasis":    _serialiseCondition(engineOutput.nephrolithiasis),
    }
