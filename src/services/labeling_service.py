"""Specimen label generation, printing, and affixed-confirmation for the
receptionist/encoder intake flow.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.encryption import decryptPii
from ..core.exceptions import SpecimenNotFoundError, UnprocessableException
from ..models.print_job import PrintJob
from ..models.sample_label import SampleLabel
from ..models.specimen import Specimen
from ..schemas.labeling import (
    LabelConfirmResponse,
    LabelPreviewData,
    PrintJobResponse,
    PrintLabelResponse,
    ReceivedSpecimenSearchItem,
)

log = logging.getLogger(__name__)

_MAX_SEARCH_RESULTS = 5
_LIKE_ESCAPE_CHAR = "\\"


def _escapeLikePattern(raw: str) -> str:
    """Escape LIKE/ILIKE metacharacters (`%`, `_`) so a receptionist's literal
    search text can't act as a wildcard — a bare `%` would otherwise match
    every `RECEIVED` specimen up to `_MAX_SEARCH_RESULTS`.

    The backslash itself must be escaped first — escaping `%`/`_` afterwards
    would double-escape any backslash the caller's text already contained.
    """
    return (
        raw.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{_LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{_LIKE_ESCAPE_CHAR}_")
    )


def _decryptPatientNameOrRaise(specimen: Specimen) -> str:
    """A printed specimen label with the wrong patient name is a patient-safety
    issue, not just a display bug — unlike search (which can drop a bad row),
    label generation must surface a decryption failure loudly rather than
    silently falling back to ciphertext or an empty string.
    """
    try:
        return decryptPii(specimen.patientName) if specimen.patientName else "Unknown"
    except Exception as exc:
        log.error(
            "Failed to decrypt patient_name for specimen_id=%s while generating a "
            "label — refusing to print with a wrong or missing name.",
            specimen.specimenId,
        )
        raise UnprocessableException(
            code="PATIENT_NAME_DECRYPTION_FAILED",
            message="Could not decrypt the patient name for this specimen. Label not generated.",
        ) from exc


async def _labelCountsFor(db: AsyncSession, specimenIds: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count `sample_labels` rows (superseded or not) per specimen, in one query.

    Returns:
        `{specimenId: count}` — a specimen with no labels yet is simply
        absent (callers should default to 0), not present with a 0 entry.
    """
    if not specimenIds:
        return {}
    stmt = (
        select(SampleLabel.specimenId, func.count())
        .where(SampleLabel.specimenId.in_(specimenIds))
        .group_by(SampleLabel.specimenId)
    )
    rows = (await db.execute(stmt)).all()
    return {specimenId: count for specimenId, count in rows}


async def _currentLabelFor(db: AsyncSession, specimenId: uuid.UUID) -> SampleLabel | None:
    """The specimen's newest, non-superseded label.

    Deterministic replacement for a bare `.first()` with no ordering, which
    — once `generateLabel` allowed more than one label row per specimen via
    regenerate — could return whichever of several rows the query happened
    to return first, not necessarily the current one.
    """
    stmt = (
        select(SampleLabel)
        .where(SampleLabel.specimenId == specimenId, SampleLabel.superseded.is_(False))
        .order_by(SampleLabel.createdAt.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def searchReceivedSpecimens(db: AsyncSession, q: str) -> list[ReceivedSpecimenSearchItem]:
    """Search `RECEIVED`-status specimens by patient UID or sample UID
    (case-insensitive substring match, done in the database).

    No longer searches or returns patient name (RA 10173 data-minimization):
    this list only needs to let the MedTech pick the right specimen by its
    non-PII identifiers — it has no reason to decrypt every `RECEIVED`
    specimen's name just to do that. `generateLabel`'s preview still carries
    `patientName` once a specific specimen is selected.

    Args:
        q: search text, matched against `patientUid`/`sampleUid`. Minimum
            length is enforced at the route (`Query(min_length=...)`), not
            re-checked here. `%`/`_` are escaped before use so they're
            matched literally rather than as SQL wildcards.

    Returns:
        Up to `_MAX_SEARCH_RESULTS` matches. Each carries `labelCount`
        (total labels generated so far, superseded or not) so the frontend
        can show "times regenerated" after selecting a specimen.
    """
    pattern = f"%{_escapeLikePattern(q.strip())}%"
    stmt = (
        select(Specimen)
        .where(
            Specimen.status == "RECEIVED",
            or_(
                Specimen.patientUid.ilike(pattern, escape=_LIKE_ESCAPE_CHAR),
                Specimen.sampleUid.ilike(pattern, escape=_LIKE_ESCAPE_CHAR),
            ),
        )
        .limit(_MAX_SEARCH_RESULTS)
    )
    rows = (await db.execute(stmt)).scalars().all()

    labelCounts = await _labelCountsFor(db, [spec.specimenId for spec in rows])

    return [
        ReceivedSpecimenSearchItem(
            specimenId=spec.specimenId,
            sampleUid=spec.sampleUid,
            patientUid=spec.patientUid,
            testType=spec.testType,
            status=spec.status,
            labelCount=labelCounts.get(spec.specimenId, 0),
        )
        for spec in rows
    ]


async def generateLabel(
    db: AsyncSession, specimenId: uuid.UUID, operatorId: uuid.UUID
) -> PrintLabelResponse:
    """Generate and record a printable label for a `RECEIVED` specimen.

    Does NOT create a print job — printing is a separate, explicit step the
    receptionist takes after reviewing the preview (see `printLabel`, `POST
    .../label/print`). If the specimen already has a label from an earlier
    call (a regenerate), that prior label (and any before it) is marked
    `superseded` first, so exactly one label per specimen is ever "current."

    Args:
        operator_id: the authenticated user recorded as the label's `generated_by`.

    Returns:
        Confirmation of the created label plus the label preview data and
        `labelCount` (total labels ever generated for this specimen,
        including superseded ones — `labelCount - 1` is "times regenerated").

    Raises:
        SpecimenNotFoundError: `specimen_id` doesn't exist.
        UnprocessableException: the specimen isn't in `RECEIVED` status, or
            the patient name fails to decrypt (`PATIENT_NAME_DECRYPTION_FAILED`
            — refuses to print a label with a wrong/missing name).
    """
    specimen = await db.get(Specimen, specimenId)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimenId))
    if specimen.status != "RECEIVED":
        raise UnprocessableException(
            code="SPECIMEN_NOT_RECEIVED",
            message=f"Specimen is in state '{specimen.status}'. Must be RECEIVED.",
        )

    patientName = _decryptPatientNameOrRaise(specimen)
    labelContent = LabelPreviewData(
        patientName=patientName,
        patientUid=specimen.patientUid,
        sampleUid=specimen.sampleUid,
        testType=specimen.testType,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    await db.execute(
        update(SampleLabel)
        .where(SampleLabel.specimenId == specimenId, SampleLabel.superseded.is_(False))
        .values(superseded=True)
    )

    label = SampleLabel(
        specimenId=specimenId,
        sampleUid=specimen.sampleUid,
        labelContentJson=labelContent.model_dump(),
        generatedBy=operatorId,
    )
    db.add(label)
    await db.flush([label])

    labelCount = (
        await db.execute(
            select(func.count())
            .select_from(SampleLabel)
            .where(SampleLabel.specimenId == specimenId)
        )
    ).scalar_one()

    await AuditLogger().record(
        eventType="LABEL_GENERATED",
        entityType="specimen",
        entityId=specimenId,
        userId=operatorId,
        detailJson={
            "labelId": str(label.labelId),
            "labelCount": labelCount,
            "regenerated": labelCount > 1,
        },
    )

    await db.commit()

    return PrintLabelResponse(
        success=True,
        labelId=label.labelId,
        printJobId=None,
        preview=labelContent,
        labelCount=labelCount,
    )


async def printLabel(
    db: AsyncSession, specimenId: uuid.UUID, operatorId: uuid.UUID
) -> PrintJobResponse:
    """Create a print job for a specimen's current (non-superseded) label —
    the "Print Label" button, now separate from label generation itself
    (see `generateLabel`'s docstring for why).

    Args:
        operator_id: the authenticated user; recorded as the `userId` on
            this action's `LABEL_PRINTED` audit entry (`print_jobs` itself
            still has no actor column — the audit log is where this is
            recorded, not the print-job row).

    Returns:
        Confirmation of the created print job.

    Raises:
        SpecimenNotFoundError: `specimen_id` doesn't exist.
        HTTPException: 400, `LABEL_NOT_FOUND`, if no label has been
            generated yet for this specimen.
    """
    specimen = await db.get(Specimen, specimenId)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimenId))

    label = await _currentLabelFor(db, specimenId)
    if label is None:
        exc = HTTPException(
            status_code=400,
            detail="No label found for this specimen. Generate a label first.",
        )
        exc.errorCode = "LABEL_NOT_FOUND"
        raise exc

    printJob = PrintJob(labelId=label.labelId, specimenId=specimenId, status="SENT")
    db.add(printJob)
    await db.flush([printJob])

    await AuditLogger().record(
        eventType="LABEL_PRINTED",
        entityType="specimen",
        entityId=specimenId,
        userId=operatorId,
        detailJson={"printJobId": str(printJob.printJobId), "labelId": str(label.labelId)},
    )

    await db.commit()

    return PrintJobResponse(
        success=True,
        printJobId=printJob.printJobId,
        labelId=label.labelId,
        status=printJob.status,
    )


async def confirmLabelAffixed(
    db: AsyncSession,
    specimenId: uuid.UUID,
    operatorId: uuid.UUID,
    offlineOverride: bool,
) -> LabelConfirmResponse:
    """Confirm a specimen's label has been physically affixed, advancing it
    to `LABELED` status.

    Args:
        operator_id: the authenticated user; recorded as `generated_by` if an
            offline-override label is created here.
        offline_override: if `True` and no label record exists yet, creates
            one on the fly (flagged `offline_override: True` in its content)
            instead of requiring `generate_label` to have run first. Skips
            the printer requirement only — the specimen must still be
            `RECEIVED`, same as the normal path.

    Returns:
        Confirmation of the status transition.

    Raises:
        SpecimenNotFoundError: `specimen_id` doesn't exist.
        UnprocessableException: `SPECIMEN_NOT_RECEIVED`, if the specimen
            isn't in `RECEIVED` status — checked before any label lookup,
            with or without `offline_override`. Also raised
            (`PATIENT_NAME_DECRYPTION_FAILED`) if the patient name fails to
            decrypt while building an offline-override label.
        HTTPException: 400, `LABEL_NOT_FOUND`, if no label exists and
            `offline_override` is `False`.
    """
    specimen = await db.get(Specimen, specimenId)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimenId))
    if specimen.status != "RECEIVED":
        raise UnprocessableException(
            code="SPECIMEN_NOT_RECEIVED",
            message=f"Specimen is in state '{specimen.status}'. Must be RECEIVED to confirm labeling.",
        )

    label = await _currentLabelFor(db, specimenId)

    if label is None:
        if not offlineOverride:
            exc = HTTPException(
                status_code=400,
                detail="No label found. Print label first, or enable offline override.",
            )
            exc.errorCode = "LABEL_NOT_FOUND"
            raise exc

        patientName = _decryptPatientNameOrRaise(specimen)
        label = SampleLabel(
            specimenId=specimenId,
            sampleUid=specimen.sampleUid,
            labelContentJson={
                "patient_name": patientName,
                "patient_uid": specimen.patientUid,
                "sample_uid": specimen.sampleUid,
                "test_type": specimen.testType,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "offline_override": True,
            },
            generatedBy=operatorId,
        )
        db.add(label)
        await db.flush([label])
        log.warning("Offline override used for specimen %s. Label: %s", specimenId, label.labelId)

    specimen.status = "LABELED"
    label.affixedConfirmed = True
    label.affixedAt = datetime.now(UTC)

    await AuditLogger().record(
        eventType="LABEL_CONFIRMED",
        entityType="specimen",
        entityId=specimenId,
        userId=operatorId,
        detailJson={"labelId": str(label.labelId), "offlineOverride": offlineOverride},
    )

    await db.commit()

    return LabelConfirmResponse(
        success=True,
        message="Specimen successfully advanced to LABELED status.",
        updatedStatus="LABELED",
        offlineOverrideUsed=offlineOverride,
    )
