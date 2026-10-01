"""Specimen intake: receiving a specimen against a lab request, listing, and
the two distinct rejection flows (receiving-desk vs. post-assignment MedTech
rejection).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.encryption import decryptPii, encryptPii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.lab_request import LabRequest
from ..models.patient import Patient
from ..models.specimen import Specimen
from ..models.specimen_rejection import SpecimenRejection
from ..schemas.specimen import (
    SpecimenListItem,
    SpecimenReceiveRequest,
    SpecimenReceiveResponse,
    SpecimenRejectResponse,
    SpecimenStartAnalysisResponse,
)
from .notification_service import NotificationService
from .specimen_access import getAssignedSpecimen

log = logging.getLogger(__name__)

_PHT = timezone(timedelta(hours=8))
# Rejection reasons (desk and MedTech), with how a notification words them.
_REJECTION_REASON_LABELS = {
    "INSUFFICIENT_VOLUME": "insufficient volume",
    "WRONG_CONTAINER": "wrong container",
    "UNLABELED": "unlabeled",
    "OTHER": "other",
}
_VALID_REJECTION_REASONS = frozenset(_REJECTION_REASON_LABELS)
_UID_GENERATION_ATTEMPTS = 5

# Result statuses that mean the result is with (or past) the supervisor: it was
# confirmed and sent for review, escalated, or approved/released. Past this
# point a rejection would strand a result the supervisor is reviewing (or has
# approved/released) on a REJECTED specimen. RETURNED_FOR_CORRECTION is *not*
# here: a returned result is back in the MedTech's hands, and returning it is
# exactly what the blocked-rejection message tells them to ask for (UROLENS-238).
_SUBMITTED_RESULT_STATUSES = {
    ResultStatus.PENDING_SUPERVISOR_APPROVAL,
    ResultStatus.CRITICAL_ESCALATED,
    ResultStatus.APPROVED,
    ResultStatus.RELEASED,
}


async def _generateSampleUid(db: AsyncSession) -> str:
    """Retry-on-collision UID generation, matching physician_service.py's
    _generate_request_uid pattern — the correct existing example in this
    codebase (real-date-based, checked against the table before use).
    """
    dateStr = datetime.now(_PHT).strftime("%Y%m%d")
    for _ in range(_UID_GENERATION_ATTEMPTS):
        uid = f"SMP-{dateStr}-{secrets.randbelow(90000) + 10000}"
        existing = await db.execute(select(Specimen.specimenId).where(Specimen.sampleUid == uid))
        if existing.scalar_one_or_none() is None:
            return uid
    exc = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to generate a unique sample UID.",
    )
    exc.errorCode = "SAMPLE_UID_GENERATION_FAILED"
    raise exc


async def receiveSpecimen(
    db: AsyncSession,
    receptionistId: uuid.UUID,
    payload: SpecimenReceiveRequest,
) -> SpecimenReceiveResponse:
    """Record a specimen against its parent lab request, either as
    `RECEIVED` (visual check passed, gets a generated `sample_uid`) or
    `REJECTED` at the receiving desk (logged to `specimen_rejections`).

    Args:
        receptionist_id: the authenticated user recorded as `received_by`.

    Returns:
        Confirmation including the specimen's ID, sample UID (if received),
        resulting status, and the parent lab request's `patientUid` (so
        Sample Labeling doesn't need a second lookup).

    Raises:
        NotFoundException: `payload.lab_request_id` doesn't exist.
        ConflictException: `SPECIMEN_ALREADY_RECEIVED`, if the lab request
            is not in `PENDING_SAMPLE` — i.e. its specimen has already been
            received or rejected at the receiving desk. Checked before any
            DB write.
        HTTPException: 400, if the visual check failed but
            `payload.rejection_reason` is missing or not one of
            `_VALID_REJECTION_REASONS`. Checked before any DB write, alongside
            the status guard above. 500, if unique sample UID generation
            fails (propagated from `_generate_sample_uid`).
    """
    labRequest = await db.get(LabRequest, payload.labRequestId)
    if labRequest is None:
        raise NotFoundException(
            code="LAB_REQUEST_NOT_FOUND", message="Parent laboratory request not found."
        )
    if labRequest.status != "PENDING_SAMPLE":
        raise ConflictException(
            code="SPECIMEN_ALREADY_RECEIVED",
            message="This lab request's specimen has already been received.",
        )

    if not payload.visualCheckPassed:
        if not payload.rejectionReason:
            exc = HTTPException(status_code=400, detail="A rejection reason code is required.")
            exc.errorCode = "REJECTION_REASON_REQUIRED"
            raise exc
        if payload.rejectionReason not in _VALID_REJECTION_REASONS:
            exc = HTTPException(
                status_code=400,
                detail=f"Invalid reason code. Must be one of: {sorted(_VALID_REJECTION_REASONS)}",
            )
            exc.errorCode = "INVALID_REJECTION_REASON"
            raise exc

    patient = await db.get(Patient, labRequest.patientId)
    pNamePlain = "Unknown"
    pUid = "N/A"
    if patient is not None:
        try:
            pNamePlain = f"{decryptPii(patient.firstName)} {decryptPii(patient.lastName)}"
        except Exception:
            log.warning(
                "Failed to decrypt patient name while receiving a specimen "
                "(patient_id=%s) — falling back to 'Unknown'.",
                patient.patientId,
            )
        pUid = patient.patientUid

    initialStatus = "RECEIVED" if payload.visualCheckPassed else "REJECTED"
    parentUpdateStatus = "SAMPLE_RECEIVED" if payload.visualCheckPassed else "PENDING_SAMPLE"

    sampleUid = await _generateSampleUid(db) if payload.visualCheckPassed else None

    specimen = Specimen(
        labRequestId=payload.labRequestId,
        sampleUid=sampleUid,
        status=initialStatus,
        visualCheckPassed=payload.visualCheckPassed,
        receivedBy=receptionistId,
        patientName=encryptPii(pNamePlain),
        patientUid=pUid,
        testType=labRequest.testType,
        priorityLevel="ROUTINE",
    )
    db.add(specimen)
    await db.flush([specimen])

    if not payload.visualCheckPassed:
        db.add(
            SpecimenRejection(
                specimenId=specimen.specimenId,
                medtechId=receptionistId,
                reasonCode=payload.rejectionReason,
                freeTextNote=payload.freeTextNote,
            )
        )

    labRequest.status = parentUpdateStatus

    await AuditLogger().record(
        db=db,
        eventType="SPECIMEN_RECEIVED" if payload.visualCheckPassed else "SPECIMEN_REJECTED",
        entityType="specimen",
        entityId=specimen.specimenId,
        userId=receptionistId,
        detailJson={
            "lab_request_id": str(payload.labRequestId),
            "status": initialStatus,
            "sample_uid": sampleUid,
            "rejection_reason": payload.rejectionReason if not payload.visualCheckPassed else None,
        },
    )

    await db.commit()
    await db.refresh(specimen)

    return SpecimenReceiveResponse(
        success=True,
        specimenId=specimen.specimenId,
        sampleUid=sampleUid,
        status=initialStatus,
        message="Specimen received and recorded successfully.",
        patientUid=pUid if pUid != "N/A" else None,
    )


async def listSpecimens(db: AsyncSession, statusFilter: str | None) -> list[SpecimenListItem]:
    """List specimens, optionally filtered by status, with decrypted patient names.

    Args:
        status_filter: if given, matched case-insensitively against
            `Specimen.status`; `None` returns all specimens.

    Returns:
        Matching specimens. A row whose `patient_name` fails to decrypt is
        excluded rather than returned with ciphertext.
    """
    stmt = select(Specimen)
    if statusFilter:
        stmt = stmt.where(Specimen.status == statusFilter.upper())
    rows = (await db.execute(stmt)).scalars().all()

    items: list[SpecimenListItem] = []
    for spec in rows:
        patientName = None
        if spec.patientName:
            try:
                patientName = decryptPii(spec.patientName)
            except Exception:
                log.warning(
                    "Failed to decrypt patient_name for specimen_id=%s — excluding "
                    "from list results rather than returning ciphertext or a guess.",
                    spec.specimenId,
                )
                continue
        items.append(
            SpecimenListItem(
                specimenId=spec.specimenId,
                labRequestId=spec.labRequestId,
                sampleUid=spec.sampleUid,
                status=spec.status,
                patientName=patientName,
                patientUid=spec.patientUid,
                testType=spec.testType,
                priorityLevel=spec.priorityLevel,
                receivedAt=spec.receivedAt,
            )
        )
    return items


async def rejectSpecimen(
    db: AsyncSession,
    specimenId: uuid.UUID,
    userId: uuid.UUID,
    reasonCode: str,
    freeTextNote: str | None,
    request: Request | None = None,
) -> SpecimenRejectResponse:
    """Reject a specimen assigned to the calling MedTech (post-assignment).

    Distinct from the receiving-desk rejection in `receiveSpecimen`, which logs
    to `specimen_rejections` instead of these columns. Allowed until the result
    is with the supervisor — including after a supervisor returns it for
    correction. The rejection and its `SPECIMEN_REJECTED` audit row commit
    together; then every active receptionist is notified (best effort) that a
    new specimen must be collected (UROLENS-238).

    Args:
        userId: the authenticated MedTech; must match the specimen's
            `medtechId` (ownership check) or the call is rejected.
        freeTextNote: optional detail; blank is stored as no note.
        request: the inbound request, for the audit row's client IP.

    Returns:
        Confirmation of the rejection, including the `rejectedAt` timestamp (UTC).

    Raises:
        UnprocessableException: `INVALID_REJECTION_REASON`, if `reasonCode`
            isn't one of `_VALID_REJECTION_REASONS`.
        SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if `specimenId` doesn't exist.
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen isn't
            assigned to `userId`.
        ConflictException: `SPECIMEN_ALREADY_REJECTED`, if it's already
            rejected; `RESULT_ALREADY_SUBMITTED`, if its result is with (or
            past) the supervisor.
    """
    if reasonCode not in _VALID_REJECTION_REASONS:
        raise UnprocessableException(
            code="INVALID_REJECTION_REASON", message=f"Invalid rejection reason: {reasonCode}."
        )

    specimen = await getAssignedSpecimen(db, specimenId, userId)
    if specimen.status == "REJECTED":
        raise ConflictException(
            code="SPECIMEN_ALREADY_REJECTED", message="Specimen is already rejected."
        )

    resultStatus = (
        await db.execute(
            select(AnalysisResult.status).where(AnalysisResult.specimenId == specimenId)
        )
    ).scalar_one_or_none()
    if resultStatus in _SUBMITTED_RESULT_STATUSES:
        raise ConflictException(
            code="RESULT_ALREADY_SUBMITTED",
            message=(
                "This specimen's result has already been submitted for supervisor "
                "review, so the specimen can no longer be rejected. Ask the "
                "supervisor to return the result if the specimen is unsuitable."
            ),
        )

    note = freeTextNote.strip() if freeTextNote and freeTextNote.strip() else None
    previousStatus = specimen.status
    rejectedAt = datetime.now(UTC)
    specimen.status = "REJECTED"
    specimen.rejectionReason = reasonCode
    specimen.rejectionNote = note
    specimen.rejectedAt = rejectedAt

    await AuditLogger().record(
        eventType="SPECIMEN_REJECTED",
        entityType="specimen",
        entityId=specimenId,
        userId=userId,
        db=db,
        detailJson={
            "reason": reasonCode,
            "has_note": note is not None,
            "previous_status": previousStatus,
            "result_status": getattr(resultStatus, "value", resultStatus),
        },
        request=request,
    )
    await db.commit()

    await _notifyReceptionistsOfRejection(db, specimen, reasonCode)

    return SpecimenRejectResponse(
        specimenId=specimenId, status="REJECTED", rejectedAt=rejectedAt.isoformat()
    )


async def _notifyReceptionistsOfRejection(db: AsyncSession, specimen: Specimen, reasonCode: str) -> None:
    # After the rejection is committed, so a notification problem can never undo
    # it, and the specimen lock isn't held while push messages go out. Best
    # effort: a failure is logged, never raised.
    try:
        await NotificationService(db).notifyReceptionistsSpecimenRejected(
            specimenId=specimen.specimenId,
            sampleUid=specimen.sampleUid or str(specimen.specimenId),
            reason=_REJECTION_REASON_LABELS[reasonCode],
        )
        await db.commit()
    except Exception:
        log.exception("Failed to notify receptionists of rejected specimen_id=%s", specimen.specimenId)
        await db.rollback()



# A MedTech's specimen moves to PROCESSING from these statuses — on "Begin
# Analysis", or on its first image upload if that reaches the server first
# (e.g. "Begin Analysis" was queued offline).
STARTABLE_SPECIMEN_STATUSES = frozenset({"ASSIGNED", "IN_QUEUE"})


async def markProcessing(
    db: AsyncSession, specimen: Specimen, userId: uuid.UUID, request: Request | None = None
) -> bool:
    """Move a startable specimen to `PROCESSING` and audit it, in `db`'s transaction.

    The caller must hold the specimen's row lock (`getAssignedSpecimen`) and
    commit. A specimen that isn't in `STARTABLE_SPECIMEN_STATUSES` is left as is.

    Returns:
        Whether the specimen moved.
    """
    if specimen.status not in STARTABLE_SPECIMEN_STATUSES:
        return False
    previousStatus = specimen.status
    specimen.status = "PROCESSING"
    await AuditLogger().record(
        eventType="SPECIMEN_ANALYSIS_STARTED",
        entityType="specimen",
        entityId=specimen.specimenId,
        userId=userId,
        db=db,
        detailJson={"previous_status": previousStatus},
        request=request,
    )
    return True


async def startAnalysis(
    db: AsyncSession,
    specimenId: uuid.UUID,
    userId: uuid.UUID,
    request: Request | None = None,
) -> SpecimenStartAnalysisResponse:
    """Move a MedTech's assigned specimen to `PROCESSING` (mobile "Begin Analysis").

    Idempotent: a specimen already `PROCESSING` is returned as-is, so a
    replayed offline sync action is harmless. The start is audited
    (`SPECIMEN_ANALYSIS_STARTED`) with the change.

    Args:
        user_id: the authenticated MedTech; must match the specimen's
            `medtech_id` (ownership check) or the call is rejected.

    Raises:
        SpecimenNotFoundError: `specimen_id` doesn't exist.
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen isn't
            assigned to `userId`.
        ConflictException: `SPECIMEN_NOT_STARTABLE`, if the specimen is in
            a status that can't move to `PROCESSING` (e.g. rejected or
            completed).
    """
    specimen = await getAssignedSpecimen(db, specimenId, userId)

    if specimen.status == "PROCESSING":
        return SpecimenStartAnalysisResponse(specimenId=specimenId, status="PROCESSING")

    if not await markProcessing(db, specimen, userId, request):
        raise ConflictException(
            code="SPECIMEN_NOT_STARTABLE",
            message=f"Specimen in status {specimen.status} cannot be started.",
        )
    await db.commit()

    return SpecimenStartAnalysisResponse(specimenId=specimenId, status="PROCESSING")
