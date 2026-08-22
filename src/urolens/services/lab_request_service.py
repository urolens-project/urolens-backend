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

import random
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


async def _generate_request_uid(db: AsyncSession) -> str:
    """Retry-on-collision UID generation — same pattern as
    app/services/physician_service.py's _generate_request_uid, the correct
    existing example in this codebase (real-date-based, checked before use).
    """
    date_str = datetime.now(_PHT).strftime("%Y%m%d")
    for _ in range(_UID_GENERATION_ATTEMPTS):
        uid = f"REQ-{date_str}-{random.randint(10000, 99999)}"
        existing = await db.execute(
            select(LabRequest.lab_request_id).where(LabRequest.request_uid == uid)
        )
        if existing.scalar_one_or_none() is None:
            return uid
    raise HTTPException(status_code=500, detail="Failed to generate a unique request UID.")


async def get_physicians(db: AsyncSession) -> list[PhysicianItem]:
    """List all active physicians, for populating a lab request's physician picker.

    Returns:
        One `PhysicianItem` per active physician user.
    """
    stmt = select(User).where(User.role == "PHYSICIAN", User.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [PhysicianItem(user_id=u.user_id, username=u.username) for u in rows]


async def search_pending_lab_requests(db: AsyncSession, q: str) -> list[LabRequestSearchItem]:
    """Search `PENDING_SAMPLE` lab requests by request UID or physician name.

    Args:
        q: search text; a leading "Dr." is stripped before matching, and the
            remainder is matched case-insensitively as a substring against
            both `request_uid` and `physician_name`.

    Returns:
        Up to `_MAX_SEARCH_RESULTS` matching lab requests.
    """
    clean_q = q.strip()
    if clean_q.lower().startswith("dr."):
        clean_q = clean_q[3:].strip()
    pattern = f"%{clean_q}%"

    stmt = (
        select(LabRequest)
        .where(
            LabRequest.status == "PENDING_SAMPLE",
            or_(
                LabRequest.request_uid.ilike(pattern),
                LabRequest.physician_name.ilike(pattern),
            ),
        )
        .limit(_MAX_SEARCH_RESULTS)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        LabRequestSearchItem(
            lab_request_id=r.lab_request_id,
            request_uid=r.request_uid,
            test_type=r.test_type,
            physician_name=r.physician_name,
            patient_id=r.patient_id,
        )
        for r in rows
    ]


async def create_lab_request(
    db: AsyncSession,
    *,
    encoded_by: uuid.UUID,
    patient_id: uuid.UUID,
    test_type: str,
    clinical_notes: str | None,
    physician_id: uuid.UUID | None,
    physician_name: str | None,
    notify_receptionists: bool = False,
    ip_address: str | None = None,
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
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise NotFoundException(code="PATIENT_NOT_FOUND", message="Patient not found.")

    request_uid = await _generate_request_uid(db)

    computed_id = physician_id
    computed_name = physician_name
    if computed_id and not computed_name:
        physician = await db.get(User, computed_id)
        if physician is not None:
            computed_name = physician.username

    lab_request = LabRequest(
        request_uid=request_uid,
        patient_id=patient_id,
        physician_id=computed_id,
        physician_name=computed_name,
        test_type=test_type.upper().replace(" ", "_"),
        clinical_notes=clinical_notes,
        status="PENDING_SAMPLE",
        encoded_by=encoded_by,
    )
    db.add(lab_request)
    await db.flush([lab_request])

    if notify_receptionists:
        await NotificationService(db).notify_active_receptionists(
            request_uid=lab_request.request_uid,
            physician_name=computed_name or "",
            lab_request_id=lab_request.lab_request_id,
        )

    await AuditLogger().record(
        event_type="REQUEST_SUBMITTED",
        entity_type="lab_request",
        entity_id=lab_request.lab_request_id,
        user_id=encoded_by,
        ip_address=ip_address,
        detail_json={"request_uid": request_uid, "patient_id": str(patient_id)},
    )

    await db.commit()
    await db.refresh(lab_request)

    return LabRequestCreateResponse(
        lab_request_id=lab_request.lab_request_id,
        request_uid=lab_request.request_uid,
        patient_id=lab_request.patient_id,
        physician_id=lab_request.physician_id,
        physician_name=lab_request.physician_name,
        test_type=lab_request.test_type,
        clinical_notes=lab_request.clinical_notes,
        status=lab_request.status,
        created_at=lab_request.created_at,
    )
