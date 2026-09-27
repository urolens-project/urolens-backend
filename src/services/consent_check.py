"""Processing-consent gate for clinical actions on a specimen (UROLENS-222, RA 10173).

Called before an image is analysed (upload) and before a result goes to the
supervisor (confirm). The patient's **latest** consent record decides:

- processing refused (`consent_process = false`) -> the action is refused;
- no consent record at all -> the action is allowed but recorded as
  `CONSENT_NOT_ON_FILE`, so the lab can backfill consent. Refusing outright
  would block patients registered before consent capture existed (13 of 38
  on 2026-09-27), and processing for diagnosis by a medical institution has
  its own lawful basis under RA 10173 s.13(f) — an explicit refusal, however,
  is always honoured.
"""
from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.exceptions import ConflictException
from src.models.consent import Consent
from src.models.lab_request import LabRequest
from src.models.specimen import Specimen


async def requireProcessingConsent(
    db: AsyncSession,
    specimen: Specimen,
    actorId: uuid.UUID,
    auditLogger: AuditLogger,
    action: str,
    request: Request | None = None,
) -> None:
    """Refuse the action if the specimen's patient refused processing.

    A missing consent record is audited as `CONSENT_NOT_ON_FILE` in `db`'s
    transaction (committed or rolled back with the action), not refused.

    Args:
        db: the action's session; the audit row joins its transaction.
        specimen: the specimen being acted on (its lab request names the patient).
        actorId: the MedTech performing the action.
        auditLogger: writes the `CONSENT_NOT_ON_FILE` row.
        action: short UPPER_SNAKE label of what is being attempted (e.g.
            `IMAGE_UPLOAD`), stored in the audit row.
        request: the inbound request, for the audit row's client IP.

    Raises:
        ConflictException: `CONSENT_REFUSED`, if the patient's latest consent
            record refuses processing.
    """
    patientId = None
    if specimen.labRequestId is not None:
        patientId = await db.scalar(
            select(LabRequest.patientId).where(LabRequest.labRequestId == specimen.labRequestId)
        )

    consent = None
    if patientId is not None:
        consent = await db.scalar(
            select(Consent)
            .where(Consent.patientId == patientId)
            .order_by(Consent.recordedAt.desc())
            .limit(1)
        )

    if consent is None:
        await auditLogger.record(
            eventType="CONSENT_NOT_ON_FILE",
            entityType="specimen",
            entityId=specimen.specimenId,
            userId=actorId,
            db=db,
            detailJson={
                "action": action,
                "patient_id": str(patientId) if patientId is not None else None,
            },
            request=request,
        )
        return

    if not consent.consentProcess:
        raise ConflictException(
            code="CONSENT_REFUSED",
            message="The patient has refused consent to processing, so this specimen can't be analysed.",
        )
