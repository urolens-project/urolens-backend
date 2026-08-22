"""Mobile-client sync: builds a full or delta snapshot of a MedTech's specimens,
queue assignments, and analysis results from Supabase.
"""
import asyncio
from datetime import UTC, datetime

from src.urolens.core.supabase import supabase

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


def _remap(row: dict, pk_col: str) -> dict:
    """Rename the DB primary key column to 'id' for the mobile client."""
    out = dict(row)
    if pk_col in out:
        out["id"] = out.pop(pk_col)
    return out


def _to_str(val) -> str | None:
    """Coerce timestamps/enums to strings safely."""
    if val is None:
        return None
    return str(val)


async def pull(user_id: str, last_synced_at: datetime | None) -> dict:
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
    is_delta = last_synced_at is not None
    ts = last_synced_at.isoformat() if is_delta else None

    # ── 1 + 2. Specimens and queue_assignments in parallel ────────────────────
    # specimens must be fetched in full (not delta-filtered) to build the
    # specimen_ids list used for analysis_results filtering.
    qa_query = supabase.table("queue_assignments").select(_QUEUE_COLS).eq("medtech_id", user_id)
    if is_delta:
        qa_query = qa_query.gt("updated_at", ts)

    spec_result, qa_result = await asyncio.gather(
        supabase.table("specimens").select(_SPECIMEN_COLS).eq("medtech_id", user_id).execute(),
        qa_query.execute(),
    )

    all_spec_rows = spec_result.data or []
    all_specimen_ids = [r["specimen_id"] for r in all_spec_rows]
    queue_assignments = [_remap(r, "assignment_id") for r in (qa_result.data or [])]

    # For delta: filter changed specimen rows in Python
    spec_rows = (
        [r for r in all_spec_rows if r.get("updated_at") and r["updated_at"] > ts]
        if is_delta
        else all_spec_rows
    )
    specimens = [_remap(r, "specimen_id") for r in spec_rows]

    # ── 3. Analysis results ───────────────────────────────────────────────────
    if all_specimen_ids:
        ar_query = supabase.table("analysis_results").select(_RESULT_COLS).in_("specimen_id", all_specimen_ids)
        if is_delta:
            ar_query = ar_query.gt("updated_at", ts)
        ar_result = await ar_query.execute()
        analysis_results = [_remap(r, "result_id") for r in (ar_result.data or [])]
    else:
        analysis_results = []

    # ── Build response ─────────────────────────────────────────────────────────
    # Full sync  → all records in created, updated = []
    # Delta sync → changed records in updated, created = []
    def make_changes(records: list) -> dict:
        if is_delta:
            return {"created": [], "updated": records}
        return {"created": records, "updated": []}

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "changes": {
            "specimens":         make_changes(specimens),
            "queue_assignments": make_changes(queue_assignments),
            "analysis_results":  make_changes(analysis_results),
        },
    }
