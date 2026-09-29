"""Mobile-client sync: builds a full or delta snapshot of a MedTech's specimens,
queue assignments, analysis results, and the manual overrides on those results.

The mobile queue (UROLENS-225) is built entirely from this payload. Each
returned-for-correction result carries the supervisor's latest reason.
Patient names are deliberately **not** sent: the app shows only the patient
code (a privacy decision in the mobile UI), so the name has no reason to be on
a MedTech's phone (RA 10173 data minimization). `patient_name` stays in the
payload as `""` so existing app versions, whose local column requires a
string, keep working.

Manual overrides (UROLENS-227) are sent so the phone shows every correction on
the MedTech's results — including a supervisor's on a returned result — not
only the ones made on that device. Existing app versions ignore the new key.
"""
import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.supabase import supabase
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.result_return import ResultReturn

# Columns to select per table — only what the mobile sync needs. patient_name is
# intentionally never read: the device shows the patient code, not the name.
_SPECIMEN_COLS = (
    "specimen_id, sample_uid, patient_uid, test_type, "
    "status, priority_level, received_at, assigned_at, medtech_id, "
    "rejection_reason, rejection_note, rejected_at, updated_at"
)
_QUEUE_COLS = "assignment_id, specimen_id, medtech_id, assigned_at, status, updated_at"
_RESULT_COLS = (
    "result_id, specimen_id, ai_findings, flagged_anomalies, "
    "smart_diagnosis, smart_diagnosis_unavailable, "
    "confirmed_at, confirmed_by, "
    "status, image_id, model_version, updated_at"
)


def _remap(row: dict, pkCol: str) -> dict:
    """Rename the DB primary key column to 'id' for the mobile client."""
    out = dict(row)
    if pkCol in out:
        out["id"] = out.pop(pkCol)
    return out


def _withoutPatientName(specimen: dict) -> dict:
    # Kept as "" (never the name) because the app's local patient_name column
    # is a required string; omitting the key or sending null would make every
    # sync look like a change.
    return {**specimen, "patient_name": ""}


async def _latestReturnReasons(db: AsyncSession, resultIds: list[str]) -> dict[str, str]:
    # Latest supervisor return reason per result (a result can be returned
    # more than once); newest first, so the first seen per result wins.
    rows = (
        await db.execute(
            select(ResultReturn.resultId, ResultReturn.reason)
            .where(ResultReturn.resultId.in_([uuid.UUID(r) for r in resultIds]))
            .order_by(ResultReturn.returnedAt.desc())
        )
    ).all()
    reasons: dict[str, str] = {}
    for resultId, reason in rows:
        reasons.setdefault(str(resultId), reason)
    return reasons


async def _manualOverrides(
    db: AsyncSession, specimenIds: list[str], since: datetime | None
) -> list[dict]:
    """Overrides on the results of `specimenIds`, oldest first, as sync rows.

    Overrides are never edited, only added (and deleted when a new image
    replaces the findings), so a delta sends those added after `since`. Values
    are numbers, as the app stores them; they're kept as text in the DB.
    """
    stmt = (
        select(ManualOverride)
        .join(AnalysisResult, ManualOverride.resultId == AnalysisResult.resultId)
        .where(AnalysisResult.specimenId.in_([uuid.UUID(s) for s in specimenIds]))
        .order_by(ManualOverride.overriddenAt)
    )
    if since is not None:
        stmt = stmt.where(ManualOverride.overriddenAt > since)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(o.overrideId),
            "result_id": str(o.resultId),
            "parameter_name": o.parameterName,
            "original_ai_value": float(o.originalAiValue),
            "corrected_value": float(o.correctedValue),
            "rationale": o.rationale,
            "medtech_id": str(o.medtechId),
            "overridden_at": o.overriddenAt.isoformat(),
        }
        for o in rows
    ]


def _toStr(val) -> str | None:
    """Coerce timestamps/enums to strings safely."""
    if val is None:
        return None
    return str(val)


async def pull(
    db: AsyncSession, userId: str, lastSyncedAt: datetime | None, request: Request | None = None
) -> dict:
    """Build a sync payload for one MedTech: their specimens, queue
    assignments, the analysis results for those specimens, and the manual
    overrides on those results.

    When the payload carries any specimens or results (patient names
    included), the pull is recorded as `SYNC_PULLED` — with the specimen IDs
    sent — in `db`'s transaction (RA 10173).

    Args:
        db: the request's session — for the return-reason and override
            lookups and the audit row.
        userId: the requesting MedTech's user ID; specimens/assignments are
            scoped to this user.
        lastSyncedAt: if given, only rows updated after this timestamp are
            returned (as `updated`); if `None`, all rows are returned (as
            `created`) — a full sync.
        request: the inbound request, for the audit row's client IP.

    Returns:
        A dict with `timestamp` (server time of this sync) and `changes`,
        keyed by table name (`specimens`, `queueAssignments`,
        `analysisResults`, `manualOverrides`), each holding `{"created": [...], "updated": [...]}`
        with the DB primary key column remapped to `"id"`. Specimens carry
        `patient_name` as `""` — the name is never sent (the app shows the
        patient code); every result carries `return_reason` (the latest
        supervisor reason when RETURNED_FOR_CORRECTION, else `None`).
        Override rows carry `result_id`, `parameter_name`,
        `original_ai_value`, `corrected_value` (numbers), `rationale`,
        `medtech_id` (the author, MedTech or supervisor) and `overridden_at`.
    """
    isDelta = lastSyncedAt is not None
    # Taken before any read: the client stores it as its next lastSyncedAt,
    # so a row updated while this pull runs is still picked up next time.
    syncedAt = datetime.now(UTC).isoformat()
    ts = lastSyncedAt.isoformat() if isDelta else None

    # ── 1 + 2. Specimens and queue_assignments in parallel ────────────────────
    # specimens must be fetched in full (not delta-filtered) to build the
    # specimen_ids list used for analysis_results filtering.
    qaQuery = supabase.table("queue_assignments").select(_QUEUE_COLS).eq("medtech_id", userId)
    if isDelta:
        qaQuery = qaQuery.gt("updated_at", ts)

    specResult, qaResult = await asyncio.gather(
        supabase.table("specimens").select(_SPECIMEN_COLS).eq("medtech_id", userId).execute(),
        qaQuery.execute(),
    )

    allSpecRows = specResult.data or []
    allSpecimenIds = [r["specimen_id"] for r in allSpecRows]
    queueAssignments = [_remap(r, "assignment_id") for r in (qaResult.data or [])]

    # For delta: filter changed specimen rows in Python
    specRows = (
        [r for r in allSpecRows if r.get("updated_at") and r["updated_at"] > ts]
        if isDelta
        else allSpecRows
    )
    specimens = [_withoutPatientName(_remap(r, "specimen_id")) for r in specRows]

    # ── 3. Analysis results ───────────────────────────────────────────────────
    if allSpecimenIds:
        arQuery = supabase.table("analysis_results").select(_RESULT_COLS).in_("specimen_id", allSpecimenIds)
        if isDelta:
            arQuery = arQuery.gt("updated_at", ts)
        arResult = await arQuery.execute()
        analysisResults = [_remap(r, "result_id") for r in (arResult.data or [])]
    else:
        analysisResults = []

    returnedIds = [
        str(r["id"]) for r in analysisResults if r.get("status") == ResultStatus.RETURNED_FOR_CORRECTION
    ]
    reasons = await _latestReturnReasons(db, returnedIds) if returnedIds else {}
    for result in analysisResults:
        result["return_reason"] = reasons.get(str(result["id"]))

    # ── 4. Manual overrides on those results ──────────────────────────────────
    manualOverrides = (
        await _manualOverrides(db, allSpecimenIds, lastSyncedAt) if allSpecimenIds else []
    )

    # ── Build response ─────────────────────────────────────────────────────────
    # Full sync  → all records in created, updated = []
    # Delta sync → changed records in updated, created = []
    def makeChanges(records: list) -> dict:
        if isDelta:
            return {"created": [], "updated": records}
        return {"created": records, "updated": []}

    if specimens or analysisResults or manualOverrides:
        # RA 10173: the payload carries patient names and results — record
        # which ones this device received, in the request's transaction.
        await AuditLogger().record(
            eventType="SYNC_PULLED",
            entityType="user",
            entityId=userId,
            userId=userId,
            db=db,
            detailJson={
                "delta": isDelta,
                "specimen_ids": [str(s["id"]) for s in specimens],
                "result_ids": [str(r["id"]) for r in analysisResults],
                "override_ids": [o["id"] for o in manualOverrides],
            },
            request=request,
        )
        await db.commit()

    return {
        "timestamp": syncedAt,
        "changes": {
            "specimens":        makeChanges(specimens),
            "queueAssignments": makeChanges(queueAssignments),
            "analysisResults":  makeChanges(analysisResults),
            "manualOverrides":  makeChanges(manualOverrides),
        },
    }
