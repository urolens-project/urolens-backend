import logging
import traceback
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.analysis_result import AnalysisResult
from ..models.smart_diagnosis_output import SmartDiagnosisOutput
from ..models.engine_error_log import EngineErrorLog
from ..core.audit_logger import AuditLogger
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

# Error codes from the AI engine contract
_RULE_ENGINE_ERROR_CODES = {
    "INVALID_CLASSIFICATION",
    "RULE_EVALUATION_FAILED",
    "CONFIG_ERROR",
}


class SmartDiagnosisService:
    """
    Triggers the AI Engineer's rule engine and persists the output (T3.1).

    Critical contract: run() MUST NEVER re-raise exceptions. Engine failure
    must not break the MedTech's confirmation flow. All exceptions are caught,
    logged to engine_error_logs, and the result is marked unavailable.
    """

    def __init__(
        self,
        audit_logger: AuditLogger,
        notif_service: NotificationService,
    ) -> None:
        self.audit_logger = audit_logger
        self.notif_service = notif_service

    async def run(
        self,
        result_id: uuid.UUID,
        db: AsyncSession,
    ) -> Optional[SmartDiagnosisOutput]:
        """
        Runs Smart Diagnosis for the given result.

        Returns the persisted SmartDiagnosisOutput on success, None on failure.
        Never raises — all exceptions are handled internally.
        """
        try:
            # SAVEPOINT isolates SmartDiagnosis from the parent confirmation
            # transaction — any SQL failure here rolls back to the savepoint
            # without aborting the parent transaction.
            async with db.begin_nested():
                result = await self._load_result(result_id, db)
                classification: dict = result.ai_findings or {}

                # Call the AI Engineer's function — owned by urolens_ai package
                from urolens_ai import generate_smart_diagnosis  # type: ignore[import]
                engine_output = generate_smart_diagnosis(classification)

                db_record = await self._persist_output(engine_output, result_id, db)

                await self.audit_logger.record(
                    event_type="SMART_DIAGNOSIS_GENERATED",
                    entity_type="analysis_result",
                    entity_id=result_id,
                    user_id=None,
                    detail_json={
                        "gout_score":   engine_output.gout.level.value,
                        "gn_score":     engine_output.glomerulonephritis.level.value,
                        "nephro_score": engine_output.nephrolithiasis.level.value,
                        "no_significant_indicators": engine_output.no_significant_indicators,
                    },
                    db=db,
                    request=None,
                )
            return db_record

        except Exception as exc:
            logger.exception(
                "Smart Diagnosis engine failed for result_id=%s: %s", result_id, exc
            )
            await self._handle_failure(result_id, exc, db)
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _load_result(
        self, result_id: uuid.UUID, db: AsyncSession
    ) -> AnalysisResult:
        # Eagerly load smart_diagnosis_output so the relationship is in a
        # "loaded" state (None for fresh results) before db.add(SmartDiagnosisOutput).
        # Without this, the autoflush that INSERTs the new SmartDiagnosisOutput
        # triggers a back-reference update on AnalysisResult.smart_diagnosis_output.
        # In async SQLAlchemy, accessing an unloaded relationship during flush
        # raises MissingGreenlet, which is caught as engine failure.
        stmt = (
            select(AnalysisResult)
            .options(selectinload(AnalysisResult.smart_diagnosis_output))
            .where(AnalysisResult.result_id == result_id)
        )
        row = await db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise ValueError(f"AnalysisResult {result_id} not found")
        return result

    async def _persist_output(
        self, engine_output, result_id: uuid.UUID, db: AsyncSession
    ) -> SmartDiagnosisOutput:
        evidence_map = _build_evidence_map(engine_output)
        record = SmartDiagnosisOutput(
            result_id=result_id,
            gout_score=engine_output.gout.level.value,
            gn_score=engine_output.glomerulonephritis.level.value,
            nephro_score=engine_output.nephrolithiasis.level.value,
            no_significant_indicators=engine_output.no_significant_indicators,
            evidence_map=evidence_map,
            engine_version=engine_output.engine_version,
            status="ATTACHED",
        )
        db.add(record)

        # Denormalize into analysis_results.smart_diagnosis so that the
        # mobile sync (which queries analysis_results directly) can read it.
        stmt = select(AnalysisResult).where(AnalysisResult.result_id == result_id)
        row = await db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is not None:
            result.smart_diagnosis = {
                "gout":               evidence_map["gout"],
                "glomerulonephritis": evidence_map["glomerulonephritis"],
                "nephrolithiasis":    evidence_map["nephrolithiasis"],
                "no_significant_indicators": engine_output.no_significant_indicators,
            }

        return record

    async def _handle_failure(
        self, result_id: uuid.UUID, exc: Exception, db: AsyncSession
    ) -> None:
        """Persists the engine error and marks the result as diagnosis-unavailable."""
        try:
            error_code = _classify_error(exc)
            async with db.begin_nested():
                error_log = EngineErrorLog(
                    result_id=result_id,
                    error_code=error_code,
                    error_message=str(exc),
                    stack_trace=traceback.format_exc(),
                )
                db.add(error_log)

                stmt = select(AnalysisResult).where(AnalysisResult.result_id == result_id)
                row = await db.execute(stmt)
                result = row.scalar_one_or_none()
                if result:
                    result.smart_diagnosis_unavailable = True

                await self.audit_logger.record(
                    event_type="ENGINE_FAILED",
                    entity_type="analysis_result",
                    entity_id=result_id,
                    user_id=None,
                    detail_json={"error_code": error_code, "error": str(exc)},
                    db=db,
                    request=None,
                )

            await self.notif_service.notify_supervisor_diagnosis_unavailable(
                result_id=result_id,
            )
        except Exception:
            logger.exception(
                "Failed to persist engine error for result_id=%s", result_id
            )


def _classify_error(exc: Exception) -> str:
    """Maps an exception to the known engine error codes."""
    code = getattr(exc, "code", None)
    if code in _RULE_ENGINE_ERROR_CODES:
        return code
    return "RULE_EVALUATION_FAILED"


def _build_evidence_map(engine_output) -> dict:
    """Serialises the per-condition evidence lists to a plain dict for JSONB storage."""
    def _serialise_condition(cond_score) -> dict:
        return {
            "level": cond_score.level.value,
            "weighted_score": cond_score.weighted_score,
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
                for e in cond_score.evidence
            ],
        }

    return {
        "gout":               _serialise_condition(engine_output.gout),
        "glomerulonephritis": _serialise_condition(engine_output.glomerulonephritis),
        "nephrolithiasis":    _serialise_condition(engine_output.nephrolithiasis),
    }
