"""Lab-request creation/search and physician lookup — the canonical
implementation for both the receptionist/encoder intake flow and the
physician-portal flow (consolidated; see changelog.md's "Duplicate
lab-request creation implementations" entry). Physician-facing callers pass
their own identity as `physician_id`/`physician_name` and
`notify_receptionists=True`; receptionist-facing callers pass whatever
physician was specified on the intake form (if any) and
`notify_receptionists=False`.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import NotFoundException
from ..models.lab_request import LabRequest
from ..models.patient import Patient
from ..models.user import User
from ..schemas.lab_request import LabRequestCreateResponse, PhysicianItem
from ..schemas.specimen import LabRequestSearchItem
from .notification_service import NotificationService

_PHT = timezone(timedelta(hours=8))
_UID_GENERATION_ATTEMPTS = 5
_MAX_SEARCH_RESULTS = 5


async def _generateRequestUid(db: AsyncSession) -> str:
    """Retry-on-collision UID generation — same pattern as
    app/services/physician_service.py's _generate_request_uid, the correct
    existing example in this codebase (real-date-based, checked before use).
    """
    dateStr = datetime.now(_PHT).strftime("%Y%m%d")
    for _ in range(_UID_GENERATION_ATTEMPTS):
        uid = f"REQ-{dateStr}-{random.randint(10000, 99999)}"
        existing = await db.execute(
            select(LabRequest.labRequestId).where(LabRequest.requestUid == uid)
        )
        if existing.scalar_one_or_none() is None:
            return uid
    raise HTTPException(status_code=500, detail="Failed to generate a unique request UID.")


async def getPhysicians(db: AsyncSession) -> list[PhysicianItem]:
    """List all active physicians, for populating a lab request's physician picker.

    Returns:
        One `PhysicianItem` per active physician user.
    """
    stmt = select(User).where(User.role == "PHYSICIAN", User.isActive.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [PhysicianItem(userId=u.userId, username=u.username) for u in rows]


async def searchPendingLabRequests(db: AsyncSession, q: str) -> list[LabRequestSearchItem]:
    """Search `PENDING_SAMPLE` lab requests by request UID or physician name.

    Args:
        q: search text; a leading "Dr." is stripped before matching, and the
            remainder is matched case-insensitively as a substring against
            both `request_uid` and `physician_name`.

    Returns:
        Up to `_MAX_SEARCH_RESULTS` matching lab requests.
    """
    cleanQ = q.strip()
    if cleanQ.lower().startswith("dr."):
        cleanQ = cleanQ[3:].strip()
    pattern = f"%{cleanQ}%"

    stmt = (
        select(LabRequest)
        .where(
            LabRequest.status == "PENDING_SAMPLE",
            or_(
                LabRequest.requestUid.ilike(pattern),
                LabRequest.physicianName.ilike(pattern),
            ),
        )
        .limit(_MAX_SEARCH_RESULTS)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        LabRequestSearchItem(
            labRequestId=r.labRequestId,
            requestUid=r.requestUid,
            testType=r.testType,
            physicianName=r.physicianName,
            patientId=r.patientId,
        )
        for r in rows
    ]


async def createLabRequest(
    db: AsyncSession,
    *,
    encodedBy: uuid.UUID,
    patientId: uuid.UUID,
    testType: str,
    clinicalNotes: str | None,
    physicianId: uuid.UUID | None,
    physicianName: str | None,
    notifyReceptionists: bool = False,
    ipAddress: str | None = None,
) -> LabRequestCreateResponse:
    """Create a new lab request in `PENDING_SAMPLE` status — the single
    implementation backing both the receptionist and physician creation
    routes.

    Args:
        encoded_by: the authenticated user recorded as `encoded_by` (the
            receptionist for the receptionist-facing route, the physician
            themselves for the physician-facing route).
        physician_id: if given without `physician_name`, the name is looked
            up from that physician's user row. The physician-facing caller
            passes its own identity here; the receptionist-facing caller
            passes whatever physician (if any) was specified on the form.
        notify_receptionists: if `True`, every active receptionist is
            notified of the new request (used by the physician-facing route,
            since receptionists still need to act on it — not used when a
            receptionist creates their own request).
        ip_address: forwarded to the audit log entry, if available.

    Returns:
        The created lab request, in the one response shape shared by both
        creation routes.

    Raises:
        NotFoundException: `patient_id` doesn't match an existing patient.
        HTTPException: 500, if a unique `request_uid` couldn't be generated
            after `_UID_GENERATION_ATTEMPTS` retries (propagated from
            `_generate_request_uid`).
    """
    patient = await db.get(Patient, patientId)
    if patient is None:
        raise NotFoundException(code="PATIENT_NOT_FOUND", message="Patient not found.")

    requestUid = await _generateRequestUid(db)

    computedId = physicianId
    computedName = physicianName
    if computedId and not computedName:
        physician = await db.get(User, computedId)
        if physician is not None:
            computedName = physician.username

    labRequest = LabRequest(
        requestUid=requestUid,
        patientId=patientId,
        physicianId=computedId,
        physicianName=computedName,
        testType=testType.upper().replace(" ", "_"),
        clinicalNotes=clinicalNotes,
        status="PENDING_SAMPLE",
        encodedBy=encodedBy,
    )
    db.add(labRequest)
    await db.flush([labRequest])

    if notifyReceptionists:
        await NotificationService(db).notifyActiveReceptionists(
            requestUid=labRequest.requestUid,
            physicianName=computedName or "",
            labRequestId=labRequest.labRequestId,
        )

    await AuditLogger().record(
        eventType="REQUEST_SUBMITTED",
        entityType="lab_request",
        entityId=labRequest.labRequestId,
        userId=encodedBy,
        ipAddress=ipAddress,
        detailJson={"request_uid": requestUid, "patient_id": str(patientId)},
    )

    await db.commit()
    await db.refresh(labRequest)

    return LabRequestCreateResponse(
        labRequestId=labRequest.labRequestId,
        requestUid=labRequest.requestUid,
        patientId=labRequest.patientId,
        physicianId=labRequest.physicianId,
        physicianName=labRequest.physicianName,
        testType=labRequest.testType,
        clinicalNotes=labRequest.clinicalNotes,
        status=labRequest.status,
        createdAt=labRequest.createdAt,
    )
