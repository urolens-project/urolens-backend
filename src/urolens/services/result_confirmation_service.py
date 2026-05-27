# Path: urolens-backend/src/urolens/services/result_confirmation_service.py
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.result_confirmation import ResultConfirmation
from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    NotFoundException,
    ConflictException,
    UnprocessableException,
)
from .smart_diagnosis_service import SmartDiagnosisService
from .notification_service import NotificationService


class ResultConfirmationService:
    """
    Owns the confirm-result transaction (T2.5).

    SRP  — one responsibility: confirm a result and trigger downstream steps.
    DIP  — depends on injected AuditLogger, SmartDiagnosisService, NotificationService.
    OCP  — new post-confirmation steps (e.g. billing trigger) can be added without
           modifying confirm_result(); extend via hooks or decorators instead.
    """

    def __init__(
        self,
        db: AsyncSession,
        audit_logger: AuditLogger,
        smart_diagnosis_service: SmartDiagnosisService,
        notif_service: NotificationService,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger
        self.smart_diagnosis = smart_diagnosis_service
        self.notif_service = notif_service

    async def confirm_result(
        self,
        result_id: uuid.UUID,
        medtech_id: uuid.UUID,
        request: Request,
    ) -> ResultConfirmation:
        """
        Confirms an analysis result and triggers Smart Diagnosis.

        Raises:
            NotFoundException: result_id does not exist.
            ConflictException: result already confirmed.
            UnprocessableException: a pending image retake blocks confirmation.
        """
        result = await self._get_result(result_id)

        # Guard: cannot confirm an already-confirmed result
        if result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL:
            raise ConflictException(
                code="RESULT_ALREADY_CONFIRMED",
                message="This result has already been confirmed.",
            )

        # Guard: pending retake blocks confirmation
        await self._validate_no_pending_retake(result)

        # Create confirmation record
        now = datetime.now(timezone.utc)
        confirmation = ResultConfirmation(
            result_id=result_id,
            medtech_id=medtech_id,
            confirmed_at=now,
        )
        self.db.add(confirmation)

        # Transition result status
        result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL
        result.confirmed_by = medtech_id
        result.confirmed_at = now

        # Cache before SmartDiagnosis: savepoint rollbacks expire ORM object
        # attributes, and async SQLAlchemy cannot lazy-reload them outside a
        # greenlet context (raises MissingGreenlet).
        specimen_id = result.specimen_id

        # Trigger Smart Diagnosis — failure MUST NOT break confirmation (ISP)
        # smart_diagnosis_service.run() catches all exceptions internally
        await self.smart_diagnosis.run(result_id=result_id, db=self.db)

        # Notify the Supervisor
        await self.notif_service.notify_supervisor_result_ready(
            result_id=result_id,
            specimen_id=specimen_id,
        )

        # Audit — always the final write, same transaction (LSP: same pattern everywhere)
        await self.audit_logger.record(
            event_type="RESULT_CONFIRMED",
            entity_type="analysis_result",
            entity_id=result_id,
            user_id=medtech_id,
            detail_json={"specimen_id": str(specimen_id)},
            db=self.db,
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(confirmation)
        return confirmation

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_result(self, result_id: uuid.UUID) -> AnalysisResult:
        stmt = select(AnalysisResult).where(AnalysisResult.result_id == result_id)
        row = await self.db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND",
                message=f"No analysis result found with id {result_id}.",
            )
        return result

    async def _validate_no_pending_retake(self, result: AnalysisResult) -> None:
        """A result with status IMAGE_RETAKE_REQUESTED cannot be confirmed."""
        if result.status == ResultStatus.IMAGE_RETAKE_REQUESTED:
            raise UnprocessableException(
                code="PENDING_RETAKE",
                message="Cannot confirm a result while an image retake is pending.",
            )