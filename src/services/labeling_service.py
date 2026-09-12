"""Specimen label generation, printing, and affixed-confirmation for the
receptionist/encoder intake flow.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.encryption import decryptPii
from ..core.exceptions import SpecimenNotFoundError, UnprocessableException
from ..models.print_job import PrintJob
from ..models.sample_label import SampleLabel
from ..models.specimen import Specimen
from ..schemas.labeling import (
    LabelConfirmResponse,
    LabelPreviewData,
    PrintLabelResponse,
    ReceivedSpecimenSearchItem,
)

log = logging.getLogger(__name__)

_MAX_SEARCH_RESULTS = 5


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


async def searchReceivedSpecimens(db: AsyncSession, q: str) -> list[ReceivedSpecimenSearchItem]:
    """Search `RECEIVED`-status specimens by Patient ID or Sample ID only —
    deliberately not by patient name, for the same reason patient search
    (patient_service.searchPatients) doesn't match on name: it would let
    staff type an arbitrary name to check whether that person has a record
    here at all, which is a real privacy leak. Both IDs are plain
    (non-encrypted) columns, so this filters in SQL directly.

    Args:
        q: search text, matched against patient_uid or sample_uid.

    Returns:
        Up to `_MAX_SEARCH_RESULTS` matches.
    """
    stmt = (
        select(Specimen)
        .where(
            Specimen.status == "RECEIVED",
            (Specimen.patientUid.ilike(f"%{q}%")) | (Specimen.sampleUid.ilike(f"%{q}%")),
        )
        .limit(_MAX_SEARCH_RESULTS)
    )
    rows = (await db.execute(stmt)).scalars().all()

    results: list[ReceivedSpecimenSearchItem] = []
    for spec in rows:
        try:
            name = decryptPii(spec.patientName) if spec.patientName else ""
        except Exception:
            log.warning(
                "Failed to decrypt patient_name for specimen_id=%s during search — "
                "excluding from results rather than returning ciphertext.",
                spec.specimenId,
            )
            continue

        results.append(
            ReceivedSpecimenSearchItem(
                specimenId=spec.specimenId,
                sampleUid=spec.sampleUid,
                patientName=name,
                patientUid=spec.patientUid,
                testType=spec.testType,
                status=spec.status,
            )
        )
    return results


async def generateLabel(
    db: AsyncSession, specimenId: uuid.UUID, operatorId: uuid.UUID
) -> PrintLabelResponse:
    """Generate and record a printable label for a `RECEIVED` specimen.

    Args:
        operator_id: the authenticated user recorded as the label's `generated_by`.

    Returns:
        Confirmation of the created label and print job, plus the label preview data.

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

    label = SampleLabel(
        specimenId=specimenId,
        sampleUid=specimen.sampleUid,
        labelContentJson=labelContent.model_dump(),
        generatedBy=operatorId,
    )
    db.add(label)
    await db.flush([label])

    printJob = PrintJob(labelId=label.labelId, specimenId=specimenId, status="SENT")
    db.add(printJob)
    await db.flush([printJob])

    await db.commit()

    return PrintLabelResponse(
        success=True,
        labelId=label.labelId,
        printJobId=printJob.printJobId,
        preview=labelContent,
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
            instead of requiring `generate_label` to have run first.

    Returns:
        Confirmation of the status transition.

    Raises:
        HTTPException: 400, if no label exists and `offline_override` is `False`.
        SpecimenNotFoundError: `specimen_id` doesn't exist (checked when a
            label must be looked up or created against it).
        UnprocessableException: the patient name fails to decrypt while
            building an offline-override label (`PATIENT_NAME_DECRYPTION_FAILED`).
    """
    stmt = select(SampleLabel).where(SampleLabel.specimenId == specimenId)
    label = (await db.execute(stmt)).scalars().first()

    if label is None:
        if not offlineOverride:
            exc = HTTPException(
                status_code=400,
                detail="No label found. Print label first, or enable offline override.",
            )
            exc.errorCode = "LABEL_NOT_FOUND"
            raise exc

        specimen = await db.get(Specimen, specimenId)
        if specimen is None:
            raise SpecimenNotFoundError(str(specimenId))

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
    else:
        specimen = await db.get(Specimen, specimenId)
        if specimen is None:
            raise SpecimenNotFoundError(str(specimenId))

    specimen.status = "LABELED"
    label.affixedConfirmed = True
    label.affixedAt = datetime.now(UTC)

    await db.commit()

    return LabelConfirmResponse(
        success=True,
        message="Specimen successfully advanced to LABELED status.",
        updatedStatus="LABELED",
        offlineOverrideUsed=offlineOverride,
    )
