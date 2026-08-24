# Path: urolens-backend/src/services/result_confirmation_service.py
"""MedTech result-confirmation transaction (T2.5): confirms an analysis
result, settles particle_classes, triggers Smart Diagnosis, and notifies the
supervisor — all as one unit of work.
"""
import uuid
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.result_confirmation import ResultConfirmation
from .notification_service import NotificationService
from .smart_diagnosis_service import SmartDiagnosisService

# All statuses that mean the result has already passed the medtech confirmation step
_ALREADY_CONFIRMED_STATUSES = {
    ResultStatus.PENDING_SUPERVISOR_APPROVAL,
    ResultStatus.APPROVED,
    ResultStatus.RELEASED,
    ResultStatus.RETURNED_FOR_CORRECTION,
    ResultStatus.CRITICAL_ESCALATED,
}


class ResultConfirmationService:
    """Owns the confirm-result transaction (T2.5).

    SRP  — one responsibility: confirm a result and trigger downstream steps.
    DIP  — depends on injected AuditLogger, SmartDiagnosisService, NotificationService.
    OCP  — new post-confirmation steps (e.g. billing trigger) can be added without
           modifying confirm_result(); extend via hooks or decorators instead.
    """

    def __init__(
        self,
        db: AsyncSession,
        auditLogger: AuditLogger,
        _smartDiagnosisService: SmartDiagnosisService,
        _notifService: NotificationService,
    ) -> None:
        self.db = db
        self.auditLogger = auditLogger
        self._smartDiagnosis = _smartDiagnosisService
        self._notifService = _notifService

    async def confirmResult(
        self,
        resultId: uuid.UUID,
        medtechId: uuid.UUID,
        request: Request,
    ) -> ResultConfirmation:
        """Confirms an analysis result and triggers Smart Diagnosis.

        Args:
            medtech_id: the authenticated user recorded as `confirmed_by`.

        Returns:
            The persisted `ResultConfirmation` row.

        Raises:
            NotFoundException: result_id does not exist.
            ConflictException: result already confirmed — either because its
                status already reflects confirmation, or because a concurrent
                double-submit hit the DB's unique constraint first.
            UnprocessableException: a pending image retake blocks confirmation.
        """
        result = await self._getResult(resultId)

        # Guard: cannot re-confirm a result that has already passed medtech confirmation
        if result.status in _ALREADY_CONFIRMED_STATUSES:
            raise ConflictException(
                code="RESULT_ALREADY_CONFIRMED",
                message="This result has already been confirmed.",
            )

        # Guard: pending retake blocks confirmation
        await self._validateNoPendingRetake(result)

        # Create confirmation record
        now = datetime.now(UTC)
        confirmation = ResultConfirmation(
            resultId=resultId,
            medtechId=medtechId,
            confirmedAt=now,
        )
        self.db.add(confirmation)

        # Flush immediately so a concurrent double-submit surfaces the unique
        # constraint violation here — before we enter SmartDiagnosis.
        # If we let begin_nested() trigger the flush later, an IntegrityError
        # poisons the session and makes SmartDiagnosis + audit logging blow up.
        try:
            await self.db.flush([confirmation])
        except IntegrityError as err:
            raise ConflictException(
                code="RESULT_ALREADY_CONFIRMED",
                message="This result has already been confirmed.",
            ) from err

        # Settle particle_classes = ai_findings merged with any MedTech overrides.
        # If no overrides exist this is a straight copy of ai_findings.
        overrides = {o.parameterName: float(o.correctedValue) for o in result.manualOverrides}
        result.particleClasses = {**result.aiFindings, **overrides}

        # Transition result status
        result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL
        result.confirmedBy = medtechId
        result.confirmedAt = now

        # Cache before SmartDiagnosis: savepoint rollbacks expire ORM object
        # attributes, and async SQLAlchemy cannot lazy-reload them outside a
        # greenlet context (raises MissingGreenlet).
        specimenId = result.specimenId

        # Trigger Smart Diagnosis — failure MUST NOT break confirmation (ISP)
        # smart_diagnosis_service.run() catches all exceptions internally
        await self._smartDiagnosis.run(resultId=resultId, db=self.db)

        # Notify the Supervisor
        await self._notifService.notifySupervisorResultReady(
            resultId=resultId,
            specimenId=specimenId,
        )

        # Audit — always the final write, same transaction (LSP: same pattern everywhere)
        await self.auditLogger.record(
            eventType="RESULT_CONFIRMED",
            entityType="analysis_result",
            entityId=resultId,
            userId=medtechId,
            detailJson={"specimen_id": str(specimenId)},
            db=self.db,
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(confirmation)
        return confirmation

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _getResult(self, resultId: uuid.UUID) -> AnalysisResult:
        # Loads the result with manual_overrides eagerly, or raises
        # NotFoundException (RESULT_NOT_FOUND) if it doesn't exist.
        stmt = select(AnalysisResult).options(
            selectinload(AnalysisResult.manualOverrides)
        ).where(AnalysisResult.resultId == resultId)
        row = await self.db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND",
                message=f"No analysis result found with id {resultId}.",
            )
        return result

    async def _validateNoPendingRetake(self, result: AnalysisResult) -> None:
        """A result with status IMAGE_RETAKE_REQUESTED cannot be confirmed."""
        if result.status == ResultStatus.IMAGE_RETAKE_REQUESTED:
            raise UnprocessableException(
                code="PENDING_RETAKE",
                message="Cannot confirm a result while an image retake is pending.",
            )
