# Path: urolens-backend/src/services/result_confirmation_service.py
"""MedTech result-confirmation transaction (T2.5): confirms an analysis
result, settles particle_classes, triggers Smart Diagnosis, and notifies the
supervisor — all as one unit of work.
"""
import logging
import uuid
from datetime import UTC, date, datetime

from fastapi import Request
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.audit_logger import AuditLogger
from ..core.encryption import decryptStoredPii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus
from ..models.patient import Patient
from ..models.result_confirmation import ResultConfirmation
from ..models.result_return import ResultReturn
from ..models.specimen import Specimen
from ..schemas.result_review import (
    ConfirmResultResponse,
    MedtechQueueSort,
    MedtechQueueStatus,
)
from .consent_check import requireProcessingConsent
from .notification_service import NotificationService
from .smart_diagnosis_service import SmartDiagnosisService
from .specimen_access import getAssignedSpecimen

logger = logging.getLogger(__name__)

# Statuses a MedTech is allowed to confirm from: the initial state, and a
# result a supervisor has sent back — confirming it again re-submits it.
_CONFIRMABLE_STATUSES = {
    ResultStatus.PENDING_CONFIRM,
    ResultStatus.RETURNED_FOR_CORRECTION,
}

# Statuses a confirmation has already moved the result into (or past). Only
# these answer RESULT_ALREADY_CONFIRMED, which clients treat as success.
_ALREADY_CONFIRMED_STATUSES = {
    ResultStatus.PENDING_SUPERVISOR_APPROVAL,
    ResultStatus.CRITICAL_ESCALATED,
    ResultStatus.APPROVED,
    ResultStatus.RELEASED,
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


# Statuses in which a result is waiting on its MedTech — the confirmation queue.
_MEDTECH_QUEUE_STATUSES = (ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION)


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
        interpretationNotes: str | None = None,
    ) -> ConfirmResultResponse:
        """Confirms an analysis result and triggers Smart Diagnosis.

        Args:
            medtechId: the authenticated user recorded as `confirmed_by`.
            interpretation_notes: optional lab notes, written straight to
                `AnalysisResult.interpretation` (UROLENS-156) — previously
                nothing in this flow ever wrote that column, so the patient
                portal's "Laboratory notes" and the result PDF's
                Interpretation section always fell back to "Pending review".
                Write-once, MedTech-only: overwritten on every confirm call
                (including a re-confirm after a Supervisor return), same as
                `confirmed_by`/`confirmed_at` below — there is deliberately
                no Supervisor-side edit path for this field.

        Returns:
            The confirmation, with `resubmitted` telling a re-confirmation of
            a supervisor-returned result apart from a first confirmation (the
            app words its success message differently — UROLENS-146).

        Raises:
            NotFoundException: `RESULT_NOT_FOUND`, if `resultId` does not exist.
            ConflictException: `RESULT_ALREADY_CONFIRMED`, if the result is
                already with or past the supervisor (or a concurrent
                double-submit hit the unique constraint first);
                `RESULT_NOT_CONFIRMABLE`, if it's in any other status that
                can't be confirmed.
            ConflictException: (`SPECIMEN_REJECTED`) the result's specimen has
                been rejected.
            UnprocessableException: `PENDING_RETAKE`, if an image retake is
                pending — including when the result's image was discarded and
                no new one uploaded yet.
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if the result's
                specimen no longer exists.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen isn't
                assigned to `medtechId`.
            ConflictException: `CONSENT_REFUSED`, if the patient refused
                consent to processing (a missing consent record is audited as
                `CONSENT_NOT_ON_FILE`, not refused).
        """
        result = await self._getResult(resultId)
        # Guard: only the MedTech the specimen is assigned to may confirm it.
        # Checked before any state guard so a non-owner learns nothing about
        # the result's status.
        specimen = await getAssignedSpecimen(self.db, result.specimenId, medtechId)
        # Re-read now that the specimen is locked: the first read only told us
        # which specimen to lock, and a concurrent upload may have reset the
        # result since.
        result = await self._getResult(resultId, fresh=True)

        # Guard: pending retake blocks confirmation (checked first so this
        # specific message wins over the generic one below).
        await self._validateNoPendingRetake(result)

        # Guard: only PENDING_CONFIRM or a supervisor-returned result can be
        # confirmed. A positive allow-list (rather than enumerating "already
        # confirmed" statuses) so a status this doesn't recognise — e.g.
        # FAILED — is rejected by default instead of silently allowed through.
        if result.status not in _CONFIRMABLE_STATUSES:
            # Mobile treats RESULT_ALREADY_CONFIRMED as success (double tap /
            # replayed offline confirm), so it must only mean what it says.
            if result.status in _ALREADY_CONFIRMED_STATUSES:
                raise ConflictException(
                    code="RESULT_ALREADY_CONFIRMED",
                    message="This result has already been confirmed.",
                )
            raise ConflictException(
                code="RESULT_NOT_CONFIRMABLE",
                message="This result can't be confirmed in its current status.",
            )

        # Guard: a rejected specimen must never reach the supervisor. Without
        # this, a MedTech who rejects the specimen after the AI ran could still
        # confirm (or replay a queued offline confirm of) its result, putting it
        # in the approval queue for a specimen the lab has thrown out.
        if specimen.status == "REJECTED":
            raise ConflictException(
                code="SPECIMEN_REJECTED",
                message=(
                    "This specimen was rejected, so its result can't be confirmed "
                    "or sent for supervisor approval."
                ),
            )
        await requireProcessingConsent(
            self.db, specimen, medtechId, self.auditLogger, action="RESULT_CONFIRM", request=request
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
                confirmationId=uuid.uuid4(),  # known before flush, for the response
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
        # `manualOverrides` is ordered oldest first, so a parameter corrected
        # more than once ends up with its latest value.
        overrides = {o.parameterName: float(o.correctedValue) for o in result.manualOverrides}
        result.particleClasses = {**result.aiFindings, **overrides}

        # Transition result status
        result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL
        result.confirmedBy = medtechId
        result.confirmedAt = now
        result.interpretation = interpretationNotes

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
        return ConfirmResultResponse(
            id=confirmation.confirmationId,
            resultId=resultId,
            confirmedBy=medtechId,
            confirmedAt=now,
            resubmitted=wasReturned,
            status=ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        )

    async def listPendingForMedtech(
        self,
        medtechId: uuid.UUID,
        page: int,
        pageSize: int,
        request: Request | None = None,
        status: MedtechQueueStatus | None = None,
        sort: MedtechQueueSort = "oldest",
    ) -> dict:
        """List results awaiting this MedTech's confirmation, recording the view.

        When the page shows any results (patient names included), the view is
        recorded as `PENDING_RESULTS_VIEWED` with the result IDs shown, in the
        same transaction (RA 10173). See `_queryPendingForMedtech` for the
        listing itself.

        Args:
            medtechId: the MedTech whose queue this is.
            page: 1-based page number.
            pageSize: rows per page.
            request: the inbound request, for the audit row's client IP.
            status: only results in this status; both queue statuses when `None`.
            sort: `"oldest"` or `"newest"` by specimen received time.

        Returns:
            A dict with `items`, `total`, `page`, `pageSize`.
        """
        listing = await self._queryPendingForMedtech(medtechId, page, pageSize, status, sort)
        if listing["items"]:
            await self.auditLogger.record(
                eventType="PENDING_RESULTS_VIEWED",
                entityType="user",
                entityId=medtechId,
                userId=medtechId,
                db=self.db,
                detailJson={"result_ids": [str(item["resultId"]) for item in listing["items"]]},
                request=request,
            )
            await self.db.commit()
        return listing

    async def _queryPendingForMedtech(
        self,
        medtechId: uuid.UUID,
        page: int,
        pageSize: int,
        status: MedtechQueueStatus | None = None,
        sort: MedtechQueueSort = "oldest",
    ) -> dict:
        """List results awaiting this MedTech's confirmation.

        Their own specimens whose result is PENDING_CONFIRM or
        RETURNED_FOR_CORRECTION (or just `status`, if given).
        Returned-for-correction results come first — they're already late —
        then by when the specimen was received (`sort`), then by result ID so
        paging is stable. Returned results carry the supervisor's latest
        `returnReason` so the MedTech knows what to fix.

        Returns:
            A dict with `items`, `total`, `page`, `pageSize`.
        """
        offset = (page - 1) * pageSize
        statuses = (status,) if status else _MEDTECH_QUEUE_STATUSES
        # A rejected specimen's result can never be confirmed (UROLENS-238).
        inQueue = (
            Specimen.medtechId == medtechId,
            AnalysisResult.status.in_(statuses),
            Specimen.status != "REJECTED",
        )

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
                .where(*inQueue)
            )
        ).scalar_one()

        returnedFirst = case((AnalysisResult.status == ResultStatus.RETURNED_FOR_CORRECTION, 0), else_=1)
        byReceived = Specimen.receivedAt.asc() if sort == "oldest" else Specimen.receivedAt.desc()
        stmt = (
            select(AnalysisResult, Specimen)
            .join(Specimen, Specimen.specimenId == AnalysisResult.specimenId)
            .where(*inQueue)
            .order_by(returnedFirst, byReceived.nulls_last(), AnalysisResult.resultId)
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
            age = _computeAge(decryptStoredPii(pat.dateOfBirth)) if pat else None
            sex = pat.sex if pat else None
            items.append(
                {
                    "resultId": ar.resultId,
                    "specimenId": ar.specimenId,
                    "sampleUid": spec.sampleUid,
                    "testType": spec.testType,
                    "priorityLevel": spec.priorityLevel,
                    "receivedAt": spec.receivedAt,
                    "patientUid": spec.patientUid or "",
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

    async def _getResult(self, resultId: uuid.UUID, fresh: bool = False) -> AnalysisResult:
        # Loads the result with manual_overrides eagerly, or raises
        # NotFoundException (RESULT_NOT_FOUND) if it doesn't exist. `fresh`
        # overwrites the already-loaded object with the current DB state.
        stmt = select(AnalysisResult).options(
            selectinload(AnalysisResult.manualOverrides)
        ).where(AnalysisResult.resultId == resultId)
        if fresh:
            stmt = stmt.execution_options(populate_existing=True)
        row = await self.db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND",
                message=f"No analysis result found with id {resultId}.",
            )
        return result

    async def _validateNoPendingRetake(self, result: AnalysisResult) -> None:
        """Refuse to confirm while a retake is pending.

        That's a result marked IMAGE_RETAKE_REQUESTED, or one whose image was
        discarded (UROLENS-230): discarding keeps the old findings until a new
        image is uploaded, and they must not reach the supervisor.
        """
        retakePending = result.status == ResultStatus.IMAGE_RETAKE_REQUESTED
        if not retakePending and result.imageId is not None:
            image = await self.db.get(Image, result.imageId)
            retakePending = image is not None and image.status != ImageStatus.ACTIVE
        if retakePending:
            raise UnprocessableException(
                code="PENDING_RETAKE",
                message="Cannot confirm a result while an image retake is pending.",
            )
