"""Mobile-client sync: builds a full or delta snapshot of a MedTech's specimens,
queue assignments, analysis results, and the manual overrides on those results.

The mobile queue (UROLENS-225) and Reports screen (UROLENS-236) are built
entirely from this payload. Each returned-for-correction result carries the
supervisor's latest reason. Patient names are deliberately **not** sent: the
app shows only the patient code (a privacy decision in the mobile UI), so the
name has no reason to be on a MedTech's phone (RA 10173 data minimization).
`patient_name` stays in the payload as `""` so existing app versions, whose
local column requires a string, keep working.

Only recent history is synced (UROLENS-236): unfinished work always, finished
samples for `HISTORY_WINDOW_DAYS` after they finished. Older ones are served by
`GET /results/medtech/history`. A delta tells the phone which samples to drop —
those that aged out since its last sync or are no longer assigned to the
MedTech — in each table's `deleted` list.

Manual overrides (UROLENS-227) are sent so the phone shows every correction on
the MedTech's results — including a supervisor's on a returned result — not
only the ones made on that device.

Read with SQLAlchemy. The previous Supabase REST version downloaded every
specimen the MedTech ever had on every sync and put every specimen ID in the
results request's URL, which fails once history grows (UROLENS-236).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from src.core.audit_logger import AuditLogger
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.queue_assignment import QueueAssignment
from src.models.result_approval import ResultApproval
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen

# How long a finished sample stays on the MedTech's phone (RA 10173 data
# minimization); older ones come from GET /results/medtech/history.
HISTORY_WINDOW_DAYS = 30

# A specimen is finished once approved (COMPLETED) or rejected; anything else
# is still work in progress and always syncs, however old.
FINISHED_SPECIMEN_STATUSES = frozenset({"COMPLETED", "REJECTED"})


def finishedAt() -> ColumnElement[datetime]:
    """When a specimen last reached a finished state, as a SQL expression.

    The latest of release, completion (approval) and rejection; falls back to
    `specimens.updated_at` for rows missing those timestamps. Needs
    `analysis_results` outer-joined on the specimen.
    """
    return func.coalesce(
        func.greatest(AnalysisResult.releasedAt, Specimen.completedAt, Specimen.rejectedAt),
        Specimen.updatedAt,
    )


def _inWindow(medtechId: uuid.UUID, cutoff: datetime) -> ColumnElement[bool]:
    # The MedTech's specimens that still belong on their phone.
    return and_(
        Specimen.medtechId == medtechId,
        or_(Specimen.status.not_in(FINISHED_SPECIMEN_STATUSES), finishedAt() >= cutoff),
    )


def _windowSpecimenIds(medtechId: uuid.UUID, cutoff: datetime) -> Select:
    return (
        select(Specimen.specimenId)
        .outerjoin(AnalysisResult, AnalysisResult.specimenId == Specimen.specimenId)
        .where(_inWindow(medtechId, cutoff))
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _str(value: object) -> str | None:
    # UUIDs and enum members as plain strings, as the app stores them.
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _specimenRow(s: Specimen) -> dict[str, Any]:
    # `patient_name` is "" (never the name) because the app's local column is a
    # required string; omitting it or sending null would look like a change.
    return {
        "id": str(s.specimenId),
        "sample_uid": s.sampleUid,
        "patient_uid": s.patientUid,
        "patient_name": "",
        "test_type": s.testType,
        "status": _str(s.status),
        "priority_level": _str(s.priorityLevel),
        "received_at": _iso(s.receivedAt),
        "assigned_at": _iso(s.assignedAt),
        "completed_at": _iso(s.completedAt),
        "medtech_id": _str(s.medtechId),
        "rejection_reason": s.rejectionReason,
        "rejection_note": s.rejectionNote,
        "rejected_at": _iso(s.rejectedAt),
        "updated_at": _iso(s.updatedAt),
    }


def _resultRow(r: AnalysisResult, approvedAt: datetime | None, returnReason: str | None) -> dict[str, Any]:
    return {
        "id": str(r.resultId),
        "specimen_id": str(r.specimenId),
        "ai_findings": r.aiFindings,
        "flagged_anomalies": r.flaggedAnomalies,
        "particle_classes": r.particleClasses,
        "smart_diagnosis": r.smartDiagnosis,
        "smart_diagnosis_unavailable": r.smartDiagnosisUnavailable,
        "confirmed_at": _iso(r.confirmedAt),
        "confirmed_by": _str(r.confirmedBy),
        "approved_at": _iso(approvedAt),
        "released_at": _iso(r.releasedAt),
        "status": _str(r.status),
        "image_id": _str(r.imageId),
        "model_version": r.modelVersion,
        "updated_at": _iso(r.updatedAt),
        "return_reason": returnReason,
    }


def _assignmentRow(a: QueueAssignment) -> dict[str, Any]:
    return {
        "id": str(a.assignmentId),
        "specimen_id": str(a.specimenId),
        "medtech_id": str(a.medtechId),
        "assigned_at": _iso(a.assignedAt),
        "status": a.status,
    }


def _overrideRow(o: ManualOverride) -> dict[str, Any]:
    # Values are numbers, as the app stores them; they're text in the DB.
    return {
        "id": str(o.overrideId),
        "result_id": str(o.resultId),
        "parameter_name": o.parameterName,
        "original_ai_value": float(o.originalAiValue),
        "corrected_value": float(o.correctedValue),
        "rationale": o.rationale,
        "medtech_id": str(o.medtechId),
        "overridden_at": _iso(o.overriddenAt),
    }


async def _specimensAndResults(
    db: AsyncSession, medtechId: uuid.UUID, cutoff: datetime, since: datetime | None
) -> tuple[list[Specimen], list[AnalysisResult]]:
    # The window's specimens and their results; on a delta, only those changed
    # since the last sync (a specimen and its result can change separately).
    stmt = (
        select(Specimen, AnalysisResult)
        .outerjoin(AnalysisResult, AnalysisResult.specimenId == Specimen.specimenId)
        .where(_inWindow(medtechId, cutoff))
        # The patient's name is never even read (RA 10173 minimization); any
        # access to it raises instead of lazily loading it.
        .options(defer(Specimen.patientName, raiseload=True))
        .order_by(Specimen.receivedAt, Specimen.specimenId)
    )
    if since is not None:
        stmt = stmt.where(or_(Specimen.updatedAt > since, AnalysisResult.updatedAt > since))
    specimens: list[Specimen] = []
    results: list[AnalysisResult] = []
    for specimen, result in (await db.execute(stmt)).all():
        if since is None or specimen.updatedAt > since:
            specimens.append(specimen)
        if result is not None and (since is None or result.updatedAt > since):
            results.append(result)
    return specimens, results


async def _latestApprovals(db: AsyncSession, resultIds: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    # When each result was (last) approved; a returned-then-resubmitted result
    # can have been approved only once, but the latest is the one that counts.
    rows = (
        await db.execute(
            select(ResultApproval.resultId, func.max(ResultApproval.approvedAt))
            .where(ResultApproval.resultId.in_(resultIds))
            .group_by(ResultApproval.resultId)
        )
    ).all()
    return dict(rows)


async def _latestReturnReasons(db: AsyncSession, resultIds: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    # Latest supervisor return reason per result (a result can be returned
    # more than once); newest first, so the first seen per result wins.
    rows = (
        await db.execute(
            select(ResultReturn.resultId, ResultReturn.reason)
            .where(ResultReturn.resultId.in_(resultIds))
            .order_by(ResultReturn.returnedAt.desc())
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for resultId, reason in rows:
        reasons.setdefault(resultId, reason)
    return reasons


async def _resultRows(db: AsyncSession, results: list[AnalysisResult]) -> list[dict[str, Any]]:
    if not results:
        return []
    ids = [r.resultId for r in results]
    approvals = await _latestApprovals(db, ids)
    returnedIds = [r.resultId for r in results if r.status == ResultStatus.RETURNED_FOR_CORRECTION]
    reasons = await _latestReturnReasons(db, returnedIds) if returnedIds else {}
    return [_resultRow(r, approvals.get(r.resultId), reasons.get(r.resultId)) for r in results]


async def _assignments(
    db: AsyncSession, medtechId: uuid.UUID, cutoff: datetime, since: datetime | None
) -> list[QueueAssignment]:
    # Assignments are only ever created, never edited (`queue_assignments` has
    # no `updated_at`), so a delta sends those made since the last sync.
    stmt = (
        select(QueueAssignment)
        .where(
            QueueAssignment.medtechId == medtechId,
            QueueAssignment.specimenId.in_(_windowSpecimenIds(medtechId, cutoff)),
        )
        .order_by(QueueAssignment.assignedAt)
    )
    if since is not None:
        stmt = stmt.where(QueueAssignment.assignedAt > since)
    return list((await db.execute(stmt)).scalars().all())


async def _manualOverrides(
    db: AsyncSession, medtechId: uuid.UUID, cutoff: datetime, since: datetime | None
) -> list[ManualOverride]:
    # Overrides on the window's results, oldest first. Never edited, only added
    # (and deleted when a new image replaces the findings), so a delta sends
    # those added since the last sync.
    stmt = (
        select(ManualOverride)
        .join(AnalysisResult, ManualOverride.resultId == AnalysisResult.resultId)
        .where(AnalysisResult.specimenId.in_(_windowSpecimenIds(medtechId, cutoff)))
        .order_by(ManualOverride.overriddenAt)
    )
    if since is not None:
        stmt = stmt.where(ManualOverride.overriddenAt > since)
    return list((await db.execute(stmt)).scalars().all())


async def _removals(
    db: AsyncSession, medtechId: uuid.UUID, cutoff: datetime, since: datetime
) -> dict[str, list[str]]:
    """IDs the phone should delete, per sync table.

    Specimens that aged out of the window since the last sync (finished
    between `since - window` and `cutoff`), and specimens the MedTech had an
    assignment for that are now assigned to someone else (or nobody) and
    changed since the last sync — with their results, the MedTech's
    assignments and the overrides on those results.
    """
    agedOut = and_(
        Specimen.medtechId == medtechId,
        Specimen.status.in_(FINISHED_SPECIMEN_STATUSES),
        finishedAt() < cutoff,
        finishedAt() >= since - timedelta(days=HISTORY_WINDOW_DAYS),
    )
    reassigned = and_(
        QueueAssignment.assignmentId.is_not(None),
        Specimen.medtechId.is_distinct_from(medtechId),
        Specimen.updatedAt > since,
    )
    rows = (
        await db.execute(
            select(Specimen.specimenId, AnalysisResult.resultId)
            .outerjoin(AnalysisResult, AnalysisResult.specimenId == Specimen.specimenId)
            .outerjoin(
                QueueAssignment,
                and_(QueueAssignment.specimenId == Specimen.specimenId, QueueAssignment.medtechId == medtechId),
            )
            .where(or_(agedOut, reassigned))
            .distinct()
        )
    ).all()
    specimenIds = sorted({specimenId for specimenId, _ in rows})
    resultIds = sorted({resultId for _, resultId in rows if resultId is not None})
    assignmentIds: list[uuid.UUID] = []
    overrideIds: list[uuid.UUID] = []
    if specimenIds:
        assignmentIds = list((await db.execute(
            select(QueueAssignment.assignmentId).where(
                QueueAssignment.medtechId == medtechId, QueueAssignment.specimenId.in_(specimenIds)
            )
        )).scalars().all())
    if resultIds:
        overrideIds = list((await db.execute(
            select(ManualOverride.overrideId).where(ManualOverride.resultId.in_(resultIds))
        )).scalars().all())
    return {
        "specimens": [str(i) for i in specimenIds],
        "queueAssignments": [str(i) for i in assignmentIds],
        "analysisResults": [str(i) for i in resultIds],
        "manualOverrides": [str(i) for i in overrideIds],
    }


async def pull(
    db: AsyncSession, userId: str, lastSyncedAt: datetime | None, request: Request | None = None
) -> dict:
    """Build a sync payload for one MedTech.

    Their unfinished specimens, and finished ones (approved or rejected) for
    `HISTORY_WINDOW_DAYS` after they finished; the queue assignments, analysis
    results and manual overrides for those specimens. When the payload carries
    any specimens, results or overrides, the pull is recorded as `SYNC_PULLED`
    — with the IDs sent — in `db`'s transaction (RA 10173).

    Args:
        db: the request's session.
        userId: the requesting MedTech's user ID; everything is scoped to it.
        lastSyncedAt: if given, only rows changed after this time are returned
            (as `updated`), plus what to remove (as `deleted`); if `None`, the
            whole window is returned (as `created`) — a full sync.
        request: the inbound request, for the audit row's client IP.

    Returns:
        A dict with `timestamp` (server time of this sync) and `changes`,
        keyed by table (`specimens`, `queueAssignments`, `analysisResults`,
        `manualOverrides`), each holding `{"created": [...], "updated":
        [...], "deleted": [ids]}`. Rows use DB column names with the primary
        key as `"id"`. Specimens carry `patient_name` as `""` and
        `completed_at`; results carry `return_reason` (latest, when
        RETURNED_FOR_CORRECTION), `approved_at`, `released_at` and
        `particle_classes` (the confirmed counts); overrides carry numbers.
        `deleted` is only filled on a delta.
    """
    medtechId = uuid.UUID(userId)
    isDelta = lastSyncedAt is not None
    # Taken before any read: the client stores it as its next lastSyncedAt,
    # so a row updated while this pull runs is still picked up next time.
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=HISTORY_WINDOW_DAYS)

    specimens, results = await _specimensAndResults(db, medtechId, cutoff, lastSyncedAt)
    specimenRows = [_specimenRow(s) for s in specimens]
    resultRows = await _resultRows(db, results)
    assignmentRows = [_assignmentRow(a) for a in await _assignments(db, medtechId, cutoff, lastSyncedAt)]
    overrideRows = [_overrideRow(o) for o in await _manualOverrides(db, medtechId, cutoff, lastSyncedAt)]
    removed = (
        await _removals(db, medtechId, cutoff, lastSyncedAt)
        if lastSyncedAt is not None
        else {"specimens": [], "queueAssignments": [], "analysisResults": [], "manualOverrides": []}
    )

    def makeChanges(records: list, table: str) -> dict:
        # Full sync → created; delta → updated. Removals only on a delta.
        return {
            "created": [] if isDelta else records,
            "updated": records if isDelta else [],
            "deleted": removed[table],
        }

    if specimenRows or resultRows or overrideRows:
        # RA 10173: record which patients' samples and results this device
        # received, in the request's transaction.
        await AuditLogger().record(
            eventType="SYNC_PULLED",
            entityType="user",
            entityId=userId,
            userId=userId,
            db=db,
            detailJson={
                "delta": isDelta,
                "specimen_ids": [s["id"] for s in specimenRows],
                "result_ids": [r["id"] for r in resultRows],
                "override_ids": [o["id"] for o in overrideRows],
                "removed_specimen_ids": removed["specimens"],
            },
            request=request,
        )
        await db.commit()

    return {
        "timestamp": now.isoformat(),
        "changes": {
            "specimens":        makeChanges(specimenRows, "specimens"),
            "queueAssignments": makeChanges(assignmentRows, "queueAssignments"),
            "analysisResults":  makeChanges(resultRows, "analysisResults"),
            "manualOverrides":  makeChanges(overrideRows, "manualOverrides"),
        },
    }
