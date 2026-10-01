"""A MedTech's full sample history, online (UROLENS-236).

The phone only keeps the last `sync_service.HISTORY_WINDOW_DAYS` of finished
samples (RA 10173 data minimization); this serves all of them, one Reports
category at a time, newest first.
"""
from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from src.core.audit_logger import AuditLogger
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.result_approval import ResultApproval
from src.models.specimen import Specimen
from src.schemas.medtech_history import (
    MedtechHistoryCategory,
    MedtechHistoryItem,
    MedtechHistoryListResponse,
)

# Which result status each result-driven Reports category shows.
_RESULT_STATUS_BY_CATEGORY: dict[str, ResultStatus] = {
    "PENDING_APPROVAL": ResultStatus.PENDING_SUPERVISOR_APPROVAL,
    "APPROVED": ResultStatus.APPROVED,
    "RELEASED": ResultStatus.RELEASED,
}


def _finalizedAt(category: MedtechHistoryCategory) -> ColumnElement:
    # When a sample reached its category; the row's update time as a fallback.
    if category == "REJECTED":
        return func.coalesce(Specimen.rejectedAt, Specimen.updatedAt)
    if category == "PENDING_APPROVAL":
        reached = AnalysisResult.confirmedAt
    elif category == "APPROVED":
        # Latest approval: a returned-then-resubmitted result is approved once.
        reached = (
            select(func.max(ResultApproval.approvedAt))
            .where(ResultApproval.resultId == AnalysisResult.resultId)
            .scalar_subquery()
        )
    else:
        reached = AnalysisResult.releasedAt
    return func.coalesce(reached, AnalysisResult.updatedAt)


def _categoryQuery(medtechId: uuid.UUID, category: MedtechHistoryCategory) -> Select:
    # The MedTech's own samples in `category`, with when each got there. The
    # patient's name is never read (patient code only); access would raise.
    finalized = _finalizedAt(category).label("finalizedAt")
    query = select(Specimen, AnalysisResult.resultId, finalized).options(
        defer(Specimen.patientName, raiseload=True)
    )
    if category == "REJECTED":
        return query.outerjoin(AnalysisResult, AnalysisResult.specimenId == Specimen.specimenId).where(
            Specimen.medtechId == medtechId, Specimen.status == "REJECTED"
        )
    return query.join(AnalysisResult, AnalysisResult.specimenId == Specimen.specimenId).where(
        Specimen.medtechId == medtechId,
        AnalysisResult.status == _RESULT_STATUS_BY_CATEGORY[category],
    )


async def listHistory(
    db: AsyncSession,
    medtechId: uuid.UUID,
    category: MedtechHistoryCategory,
    page: int,
    pageSize: int,
    request: Request | None = None,
) -> MedtechHistoryListResponse:
    """List the MedTech's samples in one Reports category, newest first.

    Only the caller's own specimens, however old — including those past the
    phone's sync window. Order is by when each sample reached the category,
    then specimen ID (stable paging). When the page shows any samples, the
    view is recorded as `MEDTECH_HISTORY_VIEWED` with the specimen IDs shown,
    in the same transaction (RA 10173).

    Args:
        db: the request's session, for the listing and the audit row.
        medtechId: the calling MedTech (from the token, never the client).
        category: `PENDING_APPROVAL`, `APPROVED`, `RELEASED` or `REJECTED`.
        page: 1-based page number.
        pageSize: rows per page.
        request: the inbound request, for the audit row's client IP.
    """
    query = _categoryQuery(medtechId, category)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(
            query.order_by(query.selected_columns.finalizedAt.desc().nulls_last(), Specimen.specimenId)
            .offset((page - 1) * pageSize)
            .limit(pageSize)
        )
    ).all()
    items = [
        MedtechHistoryItem(
            specimenId=specimen.specimenId,
            resultId=resultId,
            sampleUid=specimen.sampleUid,
            patientUid=specimen.patientUid,
            testType=specimen.testType,
            priorityLevel=getattr(specimen.priorityLevel, "value", specimen.priorityLevel),
            receivedAt=specimen.receivedAt,
            category=category,
            finalizedAt=finalized,
            rejectionReason=specimen.rejectionReason if category == "REJECTED" else None,
        )
        for specimen, resultId, finalized in rows
    ]
    if items:
        await AuditLogger().record(
            eventType="MEDTECH_HISTORY_VIEWED",
            entityType="user",
            entityId=medtechId,
            userId=medtechId,
            db=db,
            detailJson={"category": category, "specimen_ids": [str(i.specimenId) for i in items]},
            request=request,
        )
        await db.commit()
    return MedtechHistoryListResponse(items=items, total=total, page=page, pageSize=pageSize)
