"""Specimen label generation, printing, and affixed-confirmation for the
receptionist/encoder intake flow."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.encryption import decrypt_pii
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


def _decrypt_patient_name_or_raise(specimen: Specimen) -> str:
    """
    A printed specimen label with the wrong patient name is a patient-safety
    issue, not just a display bug — unlike search (which can drop a bad row),
    label generation must surface a decryption failure loudly rather than
    silently falling back to ciphertext or an empty string.
    """
    try:
        return decrypt_pii(specimen.patient_name) if specimen.patient_name else "Unknown"
    except Exception as exc:
        log.error(
            "Failed to decrypt patient_name for specimen_id=%s while generating a "
            "label — refusing to print with a wrong or missing name.",
            specimen.specimen_id,
        )
        raise UnprocessableException(
            code="PATIENT_NAME_DECRYPTION_FAILED",
            message="Could not decrypt the patient name for this specimen. Label not generated.",
        ) from exc


async def search_received_specimens(db: AsyncSession, q: str) -> list[ReceivedSpecimenSearchItem]:
    """Search `RECEIVED`-status specimens by decrypted patient name, patient
    UID, or sample UID (case-insensitive substring match).

    Fetches up to 200 candidate rows and decrypts/filters in Python, since
    patient names are encrypted at rest. A row that fails to decrypt is
    logged and excluded rather than returned with ciphertext.

    Args:
        q: search text, matched against name/patient UID/sample UID.

    Returns:
        Up to `_MAX_SEARCH_RESULTS` matches, in the order scanned.
    """
    stmt = select(Specimen).where(Specimen.status == "RECEIVED").limit(200)
    rows = (await db.execute(stmt)).scalars().all()

    q_lower = q.strip().lower()
    results: list[ReceivedSpecimenSearchItem] = []
    for spec in rows:
        try:
            name = decrypt_pii(spec.patient_name) if spec.patient_name else ""
        except Exception:
            log.warning(
                "Failed to decrypt patient_name for specimen_id=%s during search — "
                "excluding from results rather than returning ciphertext.",
                spec.specimen_id,
            )
            continue

        uid = spec.patient_uid or ""
        sample_uid = spec.sample_uid or ""
        if q_lower in name.lower() or q_lower in uid.lower() or q_lower in sample_uid.lower():
            results.append(
                ReceivedSpecimenSearchItem(
                    specimen_id=spec.specimen_id,
                    sample_uid=spec.sample_uid,
                    patient_name=name,
                    patient_uid=spec.patient_uid,
                    test_type=spec.test_type,
                    status=spec.status,
                )
            )
        if len(results) == _MAX_SEARCH_RESULTS:
            break
    return results


async def generate_label(
    db: AsyncSession, specimen_id: uuid.UUID, operator_id: uuid.UUID
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
    specimen = await db.get(Specimen, specimen_id)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimen_id))
    if specimen.status != "RECEIVED":
        raise UnprocessableException(
            code="SPECIMEN_NOT_RECEIVED",
            message=f"Specimen is in state '{specimen.status}'. Must be RECEIVED.",
        )

    patient_name = _decrypt_patient_name_or_raise(specimen)
    label_content = LabelPreviewData(
        patient_name=patient_name,
        patient_uid=specimen.patient_uid,
        sample_uid=specimen.sample_uid,
        test_type=specimen.test_type,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    label = SampleLabel(
        specimen_id=specimen_id,
        sample_uid=specimen.sample_uid,
        label_content_json=label_content.model_dump(),
        generated_by=operator_id,
    )
    db.add(label)
    await db.flush([label])

    print_job = PrintJob(label_id=label.label_id, specimen_id=specimen_id, status="SENT")
    db.add(print_job)
    await db.flush([print_job])

    await db.commit()

    return PrintLabelResponse(
        success=True,
        label_id=label.label_id,
        print_job_id=print_job.print_job_id,
        preview=label_content,
    )


async def confirm_label_affixed(
    db: AsyncSession,
    specimen_id: uuid.UUID,
    operator_id: uuid.UUID,
    offline_override: bool,
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
    stmt = select(SampleLabel).where(SampleLabel.specimen_id == specimen_id)
    label = (await db.execute(stmt)).scalars().first()

    if label is None:
        if not offline_override:
            raise HTTPException(
                status_code=400,
                detail="No label found. Print label first, or enable offline override.",
            )

        specimen = await db.get(Specimen, specimen_id)
        if specimen is None:
            raise SpecimenNotFoundError(str(specimen_id))

        patient_name = _decrypt_patient_name_or_raise(specimen)
        label = SampleLabel(
            specimen_id=specimen_id,
            sample_uid=specimen.sample_uid,
            label_content_json={
                "patient_name": patient_name,
                "patient_uid": specimen.patient_uid,
                "sample_uid": specimen.sample_uid,
                "test_type": specimen.test_type,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "offline_override": True,
            },
            generated_by=operator_id,
        )
        db.add(label)
        await db.flush([label])
        log.warning("Offline override used for specimen %s. Label: %s", specimen_id, label.label_id)
    else:
        specimen = await db.get(Specimen, specimen_id)
        if specimen is None:
            raise SpecimenNotFoundError(str(specimen_id))

    specimen.status = "LABELED"
    label.affixed_confirmed = True
    label.affixed_at = datetime.now(timezone.utc)

    await db.commit()

    return LabelConfirmResponse(
        success=True,
        message="Specimen successfully advanced to LABELED status.",
        updated_status="LABELED",
        offline_override_used=offline_override,
    )
