# Path: urolens-backend/src/services/result_confirmation_service.py
"""MedTech result-confirmation transaction (T2.5): confirms an analysis
result, settles particle_classes, triggers Smart Diagnosis, and notifies the
supervisor — all as one unit of work.
"""
import logging
import uuid
from datetime import UTC, date, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.audit_logger import AuditLogger
from ..core.encryption import decryptPii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.patient import Patient
from ..models.result_confirmation import ResultConfirmation
from ..models.result_return import ResultReturn
from ..models.specimen import Specimen
from .notification_service import NotificationService
from .smart_diagnosis_service import SmartDiagnosisService

logger = logging.getLogger(__name__)

# Statuses a MedTech is allowed to confirm from: the initial state, and a
# result a supervisor has sent back — confirming it again re-submits it.
_CONFIRMABLE_STATUSES = {
    ResultStatus.PENDING_CONFIRM,
    ResultStatus.RETURNED_FOR_CORRECTION,
}


def _computeAge(dobStr: str | None) -> int | None:
    if not dobStr:
        return None
    try:
        dob = date.fromisoformat(dobStr[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _decryptOrNone(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return decryptPii(ciphertext)
    except Exception:
        return None


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
            ConflictException: result isn't in a confirmable status — either
                because it already passed confirmation, or because a
                concurrent double-submit hit the DB's unique constraint
                first.
            UnprocessableException: a pending image retake blocks confirmation.
        """
        result = await self._getResult(resultId)

        # Guard: pending retake blocks confirmation (checked first so this
        # specific message wins over the generic one below).
        await self._validateNoPendingRetake(result)

        # Guard: only PENDING_CONFIRM or a supervisor-returned result can be
        # confirmed. A positive allow-list (rather than enumerating "already
        # confirmed" statuses) so a status this doesn't recognise — e.g.
        # FAILED — is rejected by default instead of silently allowed through.
        if result.status not in _CONFIRMABLE_STATUSES:
            raise ConflictException(
                code="RESULT_ALREADY_CONFIRMED",
                message="This result has already been confirmed.",
            )

        wasReturned = result.status == ResultStatus.RETURNED_FOR_CORRECTION

        # Create (or, if one already exists — the normal case on
        # re-confirmation after a supervisor return, and defensively for any
        # other state where a confirmation row outlived a status change)
        # update the confirmation record in place. result_confirmations.
        # result_id is UNIQUE, so a second INSERT for the same result would
        # violate that constraint.
        now = datetime.now(UTC)
        existing = await self.db.execute(
            select(ResultConfirmation).where(ResultConfirmation.resultId == resultId)
        )
        confirmation = existing.scalar_one_or_none()

        if confirmation is not None:
            confirmation.medtechId = medtechId
            confirmation.confirmedAt = now
        else:
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

        # Notify the Supervisor — best-effort, same as Smart Diagnosis above:
        # a transient notification failure (e.g. push/email provider down)
        # must not fail an otherwise-valid confirmation.
        try:
            await self._notifService.notifySupervisorResultReady(
                resultId=resultId,
                specimenId=specimenId,
            )
        except Exception:
            logger.exception(
                "Failed to notify supervisor for result %s (confirmation still recorded)",
                resultId,
            )

        # Audit — always the final write, same transaction (LSP: same pattern everywhere)
        await self.auditLogger.record(
            eventType="RESULT_RESUBMITTED" if wasReturned else "RESULT_CONFIRMED",
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

    async def listPendingForMedtech(
        self, medtechId: uuid.UUID, page: int, pageSize: int
    ) -> dict:
        """Lists results awaiting this MedTech's confirmation: their own
        specimens with status PENDING_CONFIRM or RETURNED_FOR_CORRECTION,
        oldest first. Returned-for-correction results carry the
        supervisor's `returnReason` so the MedTech knows what to fix.

        Returns:
            A dict with `items`, `total`, `page`, `pageSize`.
        """
        offset = (page - 1) * pageSize
        statuses = (ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION)

        countStmt = (
            select(AnalysisResult.resultId)
            .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
            .where(Specimen.medtechId == medtechId, AnalysisResult.status.in_(statuses))
        )
        total = len((await self.db.execute(countStmt)).all())

        stmt = (
            select(AnalysisResult, Specimen)
            .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
            .where(Specimen.medtechId == medtechId, AnalysisResult.status.in_(statuses))
            .order_by(AnalysisResult.status.desc(), AnalysisResult.resultId)
            .offset(offset)
            .limit(pageSize)
        )
        rows = (await self.db.execute(stmt)).all()
        if not rows:
            return {"items": [], "total": total, "page": page, "pageSize": pageSize}

        returnedResultIds = {ar.resultId for ar, _ in rows if ar.status == ResultStatus.RETURNED_FOR_CORRECTION}
        reasonMap: dict[uuid.UUID, str] = {}
        if returnedResultIds:
            retRows = (
                await self.db.execute(
                    select(ResultReturn)
                    .where(ResultReturn.resultId.in_(returnedResultIds))
                    .order_by(ResultReturn.returnedAt.desc())
                )
            ).scalars().all()
            for ret in retRows:
                reasonMap.setdefault(ret.resultId, ret.reason)

        patientUids = list({s.patientUid for _, s in rows if s.patientUid})
        patMap: dict[str, Patient] = {}
        if patientUids:
            patRows = (
                await self.db.execute(select(Patient).where(Patient.patientUid.in_(patientUids)))
            ).scalars().all()
            patMap = {p.patientUid: p for p in patRows}

        items = []
        for ar, spec in rows:
            pat = patMap.get(spec.patientUid) if spec.patientUid else None
            name = (_decryptOrNone(spec.patientName) or "") if spec else ""
            age = _computeAge(_decryptOrNone(pat.dateOfBirth)) if pat else None
            sex = pat.sex if pat else None
            items.append(
                {
                    "resultId": ar.resultId,
                    "specimenId": ar.specimenId,
                    "patientUid": spec.patientUid or "",
                    "patientName": name,
                    "patientAge": age,
                    "patientSex": sex,
                    "status": ar.status,
                    "returnReason": reasonMap.get(ar.resultId),
                }
            )

        return {"items": items, "total": total, "page": page, "pageSize": pageSize}

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
