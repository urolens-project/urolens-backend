"""Mobile-client sync: builds a full or delta snapshot of a MedTech's specimens,
queue assignments, and analysis results from Supabase.
"""
import asyncio
from datetime import UTC, datetime

from src.core.supabase import supabase

# Columns to select per table — only what the mobile sync needs
_SPECIMEN_COLS = (
    "specimen_id, sample_uid, patient_name, patient_uid, test_type, "
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


def _toStr(val) -> str | None:
    """Coerce timestamps/enums to strings safely."""
    if val is None:
        return None
    return str(val)


async def pull(userId: str, lastSyncedAt: datetime | None) -> dict:
    """Build a sync payload for one MedTech: their specimens, queue
    assignments, and the analysis results for those specimens.

    Args:
        user_id: the requesting MedTech's user ID; specimens/assignments are
            scoped to this user.
        last_synced_at: if given, only rows updated after this timestamp are
            returned (as `updated`); if `None`, all rows are returned (as
            `created`) — a full sync.

    Returns:
        A dict with `timestamp` (server time of this sync) and `changes`,
        keyed by table name (`specimens`, `queue_assignments`,
        `analysis_results`), each holding `{"created": [...], "updated": [...]}`
        with the DB primary key column remapped to `"id"`.
    """
    isDelta = lastSyncedAt is not None
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
    specimens = [_remap(r, "specimen_id") for r in specRows]

    # ── 3. Analysis results ───────────────────────────────────────────────────
    if allSpecimenIds:
        arQuery = supabase.table("analysis_results").select(_RESULT_COLS).in_("specimen_id", allSpecimenIds)
        if isDelta:
            arQuery = arQuery.gt("updated_at", ts)
        arResult = await arQuery.execute()
        analysisResults = [_remap(r, "result_id") for r in (arResult.data or [])]
    else:
        analysisResults = []

    # ── Build response ─────────────────────────────────────────────────────────
    # Full sync  → all records in created, updated = []
    # Delta sync → changed records in updated, created = []
    def makeChanges(records: list) -> dict:
        if isDelta:
            return {"created": [], "updated": records}
        return {"created": records, "updated": []}

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "changes": {
            "specimens":         makeChanges(specimens),
            "queue_assignments": makeChanges(queueAssignments),
            "analysis_results":  makeChanges(analysisResults),
        },
    }
