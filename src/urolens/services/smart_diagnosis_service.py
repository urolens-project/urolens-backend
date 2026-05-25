# Path: urolens-backend/src/urolens/services/smart_diagnosis_service.py
import uuid
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.analysis_result import AnalysisResult
from ..models.smart_diagnosis_output import SmartDiagnosisOutput, ScoreLevel
from ..models.engine_error_log import EngineErrorLog
from ..core.audit_logger import AuditLogger
from .notification_service import NotificationService

logger = logging.getLogger(__name__)


class SmartDiagnosisService:
    """
    Triggers the AI Engineer's rule engine and persists the output (T3.1).

    ISP  — this service has a single interface: run(). Callers (result_confirmation_service)
           depend only on that one method — they don't need to know how the engine works.

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
                user_id=None,  # system-generated
                detail_json={
                    "gout_score":   engine_output.gout_score,
                    "gn_score":     engine_output.gn_score,
                    "nephro_score": engine_output.nephro_score,
                },
                db=db,
                request=None,
            )
            return db_record

        except Exception as exc:
            # Engine failure must not propagate — log and mark unavailable
            logger.exception(
                "Smart Diagnosis engine failed for result_id=%s: %s", result_id, exc
            )
            await self._handle_failure(result_id, str(exc), db)
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_result(
        self, result_id: uuid.UUID, db: AsyncSession
    ) -> AnalysisResult:
        stmt = select(AnalysisResult).where(AnalysisResult.id == result_id)
        row = await db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise ValueError(f"AnalysisResult {result_id} not found")
        return result

    async def _persist_output(
        self, engine_output, result_id: uuid.UUID, db: AsyncSession
    ) -> SmartDiagnosisOutput:
        record = SmartDiagnosisOutput(
            result_id=result_id,
            gout_score=ScoreLevel(engine_output.gout_score),
            gn_score=ScoreLevel(engine_output.gn_score),
            nephro_score=ScoreLevel(engine_output.nephro_score),
            no_significant_indicators=all(
                s == ScoreLevel.LOW
                for s in (
                    ScoreLevel(engine_output.gout_score),
                    ScoreLevel(engine_output.gn_score),
                    ScoreLevel(engine_output.nephro_score),
                )
            ),
            evidence_map=dict(engine_output.evidence_map),
        )
        db.add(record)
        return record

    async def _handle_failure(
        self, result_id: uuid.UUID, error_message: str, db: AsyncSession
    ) -> None:
        """Persists the engine error and marks the result as diagnosis-unavailable."""
        try:
            # Log the engine error
            error_log = EngineErrorLog(
                result_id=result_id,
                error_message=error_message,
            )
            db.add(error_log)

            # Mark result so Supervisor sees the unavailable state
            stmt = select(AnalysisResult).where(AnalysisResult.id == result_id)
            row = await db.execute(stmt)
            result = row.scalar_one_or_none()
            if result:
                result.smart_diagnosis_unavailable = True

            # Notify Supervisor that diagnosis is unavailable
            await self.notif_service.notify_supervisor_diagnosis_unavailable(
                result_id=result_id
            )

            await self.audit_logger.record(
                event_type="ENGINE_FAILED",
                entity_type="analysis_result",
                entity_id=result_id,
                user_id=None,
                detail_json={"error": error_message},
                db=db,
                request=None,
            )
        except Exception:
            # Swallow secondary failures — primary concern is not breaking confirm
            logger.exception(
                "Failed to persist engine error for result_id=%s", result_id
            )