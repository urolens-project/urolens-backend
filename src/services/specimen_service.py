"""Specimen intake: receiving a specimen against a lab request, listing, and
the two distinct rejection flows (receiving-desk vs. post-assignment MedTech
rejection).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.encryption import decryptPii, encryptPii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    SpecimenNotFoundError,
    UnprocessableException,
)
from ..models.lab_request import LabRequest
from ..models.patient import Patient
from ..models.specimen import Specimen
from ..models.specimen_rejection import SpecimenRejection
from ..schemas.specimen import (
    SpecimenListItem,
    SpecimenReceiveRequest,
    SpecimenReceiveResponse,
    SpecimenRejectResponse,
)

log = logging.getLogger(__name__)

_PHT = timezone(timedelta(hours=8))
_VALID_REJECTION_REASONS = {"INSUFFICIENT_VOLUME", "WRONG_CONTAINER", "UNLABELED", "OTHER"}
_UID_GENERATION_ATTEMPTS = 5


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
        and resulting status.

    Raises:
        NotFoundException: `payload.lab_request_id` doesn't exist.
        HTTPException: 400, if the visual check failed but
            `payload.rejection_reason` is missing or not one of
            `_VALID_REJECTION_REASONS`. 500, if unique sample UID generation
            fails (propagated from `_generate_sample_uid`).
    """
    labRequest = await db.get(LabRequest, payload.labRequestId)
    if labRequest is None:
        raise NotFoundException(
            code="LAB_REQUEST_NOT_FOUND", message="Parent laboratory request not found."
        )

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
        db.add(
            SpecimenRejection(
                specimenId=specimen.specimenId,
                medtechId=receptionistId,
                reasonCode=payload.rejectionReason,
                freeTextNote=payload.freeTextNote,
            )
        )

    labRequest.status = parentUpdateStatus

    await db.commit()
    await db.refresh(specimen)

    return SpecimenReceiveResponse(
        success=True,
        specimenId=specimen.specimenId,
        sampleUid=sampleUid,
        status=initialStatus,
        message="Specimen received and recorded successfully.",
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
) -> SpecimenRejectResponse:
    """Post-assignment MedTech rejection of an already-received specimen.

    Ported from app/services/specimen_service.py (Track A2 audit confirmed this
    was the only surviving logic in that module — its auth pattern was
    ownership-check-in-service, not a role gate, so nothing else carried over).
    Distinct from the receiving-desk rejection in receive_specimen() above,
    which logs to specimen_rejections instead of these columns.

    Args:
        user_id: the authenticated MedTech; must match the specimen's
            `medtech_id` (ownership check) or the call is rejected.

    Returns:
        Confirmation of the rejection, including the `rejected_at` timestamp.

    Raises:
        HTTPException: 422, if `reason_code` isn't a valid reason. 403, if
            the specimen isn't assigned to `user_id`.
        SpecimenNotFoundError: `specimen_id` doesn't exist.
        ConflictException: the specimen is already rejected.
    """
    if reasonCode not in _VALID_REJECTION_REASONS:
        raise UnprocessableException(
            code="INVALID_REJECTION_REASON", message=f"Invalid rejection reason: {reasonCode}."
        )

    specimen = await db.get(Specimen, specimenId)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimenId))

    if specimen.medtechId != userId:
        exc = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Specimen is not assigned to you.",
        )
        exc.errorCode = "SPECIMEN_NOT_ASSIGNED"
        raise exc
    if specimen.status == "REJECTED":
        raise ConflictException(
            code="SPECIMEN_ALREADY_REJECTED", message="Specimen is already rejected."
        )

    rejectedAt = datetime.now(_PHT)
    specimen.status = "REJECTED"
    specimen.rejectionReason = reasonCode
    specimen.rejectionNote = freeTextNote
    specimen.rejectedAt = rejectedAt

    await db.commit()

    return SpecimenRejectResponse(
        specimenId=specimenId, status="REJECTED", rejectedAt=rejectedAt.isoformat()
    )
