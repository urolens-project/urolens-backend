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

from ..core.encryption import decrypt_pii, encrypt_pii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    SpecimenNotFoundError,
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


async def _generate_sample_uid(db: AsyncSession) -> str:
    """Retry-on-collision UID generation, matching physician_service.py's
    _generate_request_uid pattern — the correct existing example in this
    codebase (real-date-based, checked against the table before use).
    """
    date_str = datetime.now(_PHT).strftime("%Y%m%d")
    for _ in range(_UID_GENERATION_ATTEMPTS):
        uid = f"SMP-{date_str}-{secrets.randbelow(90000) + 10000}"
        existing = await db.execute(select(Specimen.specimen_id).where(Specimen.sample_uid == uid))
        if existing.scalar_one_or_none() is None:
            return uid
    raise HTTPException(status_code=500, detail="Failed to generate a unique sample UID.")


async def receive_specimen(
    db: AsyncSession,
    receptionist_id: uuid.UUID,
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
    lab_request = await db.get(LabRequest, payload.lab_request_id)
    if lab_request is None:
        raise NotFoundException(
            code="LAB_REQUEST_NOT_FOUND", message="Parent laboratory request not found."
        )

    patient = await db.get(Patient, lab_request.patient_id)
    p_name_plain = "Unknown"
    p_uid = "N/A"
    if patient is not None:
        try:
            p_name_plain = f"{decrypt_pii(patient.first_name)} {decrypt_pii(patient.last_name)}"
        except Exception:
            log.warning(
                "Failed to decrypt patient name while receiving a specimen "
                "(patient_id=%s) — falling back to 'Unknown'.",
                patient.patient_id,
            )
        p_uid = patient.patient_uid

    initial_status = "RECEIVED" if payload.visual_check_passed else "REJECTED"
    parent_update_status = "SAMPLE_RECEIVED" if payload.visual_check_passed else "PENDING_SAMPLE"

    sample_uid = await _generate_sample_uid(db) if payload.visual_check_passed else None

    specimen = Specimen(
        lab_request_id=payload.lab_request_id,
        sample_uid=sample_uid,
        status=initial_status,
        visual_check_passed=payload.visual_check_passed,
        received_by=receptionist_id,
        patient_name=encrypt_pii(p_name_plain),
        patient_uid=p_uid,
        test_type=lab_request.test_type,
        priority_level="ROUTINE",
    )
    db.add(specimen)
    await db.flush([specimen])

    if not payload.visual_check_passed:
        if not payload.rejection_reason:
            raise HTTPException(
                status_code=400, detail="A rejection reason code is required."
            )
        if payload.rejection_reason not in _VALID_REJECTION_REASONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid reason code. Must be one of: {sorted(_VALID_REJECTION_REASONS)}",
            )
        db.add(
            SpecimenRejection(
                specimen_id=specimen.specimen_id,
                medtech_id=receptionist_id,
                reason_code=payload.rejection_reason,
                free_text_note=payload.free_text_note,
            )
        )

    lab_request.status = parent_update_status

    await db.commit()
    await db.refresh(specimen)

    return SpecimenReceiveResponse(
        success=True,
        specimen_id=specimen.specimen_id,
        sample_uid=sample_uid,
        status=initial_status,
        message="Specimen received and recorded successfully.",
    )


async def list_specimens(db: AsyncSession, status_filter: str | None) -> list[SpecimenListItem]:
    """List specimens, optionally filtered by status, with decrypted patient names.

    Args:
        status_filter: if given, matched case-insensitively against
            `Specimen.status`; `None` returns all specimens.

    Returns:
        Matching specimens. A row whose `patient_name` fails to decrypt is
        excluded rather than returned with ciphertext.
    """
    stmt = select(Specimen)
    if status_filter:
        stmt = stmt.where(Specimen.status == status_filter.upper())
    rows = (await db.execute(stmt)).scalars().all()

    items: list[SpecimenListItem] = []
    for spec in rows:
        patient_name = None
        if spec.patient_name:
            try:
                patient_name = decrypt_pii(spec.patient_name)
            except Exception:
                log.warning(
                    "Failed to decrypt patient_name for specimen_id=%s — excluding "
                    "from list results rather than returning ciphertext or a guess.",
                    spec.specimen_id,
                )
                continue
        items.append(
            SpecimenListItem(
                specimen_id=spec.specimen_id,
                lab_request_id=spec.lab_request_id,
                sample_uid=spec.sample_uid,
                status=spec.status,
                patient_name=patient_name,
                patient_uid=spec.patient_uid,
                test_type=spec.test_type,
                priority_level=spec.priority_level,
                received_at=spec.received_at,
            )
        )
    return items


async def reject_specimen(
    db: AsyncSession,
    specimen_id: uuid.UUID,
    user_id: uuid.UUID,
    reason_code: str,
    free_text_note: str | None,
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
    if reason_code not in _VALID_REJECTION_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid rejection reason: {reason_code}.",
        )

    specimen = await db.get(Specimen, specimen_id)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimen_id))

    if specimen.medtech_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Specimen is not assigned to you.",
        )
    if specimen.status == "REJECTED":
        raise ConflictException(
            code="SPECIMEN_ALREADY_REJECTED", message="Specimen is already rejected."
        )

    rejected_at = datetime.now(_PHT)
    specimen.status = "REJECTED"
    specimen.rejection_reason = reason_code
    specimen.rejection_note = free_text_note
    specimen.rejected_at = rejected_at

    await db.commit()

    return SpecimenRejectResponse(
        specimen_id=specimen_id, status="REJECTED", rejected_at=rejected_at.isoformat()
    )
