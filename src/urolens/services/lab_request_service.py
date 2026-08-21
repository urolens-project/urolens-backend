from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.lab_request import LabRequest
from ..models.user import User
from ..schemas.lab_request import LabRequestCreateRequest, LabRequestCreateResponse, PhysicianItem
from ..schemas.specimen import LabRequestSearchItem

_PHT = timezone(timedelta(hours=8))
_UID_GENERATION_ATTEMPTS = 5
_MAX_SEARCH_RESULTS = 5


async def _generate_request_uid(db: AsyncSession) -> str:
    """Retry-on-collision UID generation — same pattern as
    app/services/physician_service.py's _generate_request_uid, the correct
    existing example in this codebase (real-date-based, checked before use)."""
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
    stmt = select(User).where(User.role == "PHYSICIAN", User.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [PhysicianItem(user_id=u.user_id, username=u.username) for u in rows]


async def search_pending_lab_requests(db: AsyncSession, q: str) -> list[LabRequestSearchItem]:
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
    encoder_id: uuid.UUID,
    payload: LabRequestCreateRequest,
) -> LabRequestCreateResponse:
    request_uid = await _generate_request_uid(db)

    computed_id = payload.physician_id
    computed_name = payload.physician_name
    if computed_id and not computed_name:
        physician = await db.get(User, computed_id)
        if physician is not None:
            computed_name = physician.username

    lab_request = LabRequest(
        request_uid=request_uid,
        patient_id=payload.patient_id,
        physician_id=computed_id,
        physician_name=computed_name,
        test_type=payload.test_type.upper().replace(" ", "_"),
        clinical_notes=payload.clinical_notes,
        status="PENDING_SAMPLE",
        encoded_by=encoder_id,
    )
    db.add(lab_request)
    await db.commit()
    await db.refresh(lab_request)

    return LabRequestCreateResponse(
        success=True,
        request_id=request_uid,
        message="Lab request created successfully.",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
