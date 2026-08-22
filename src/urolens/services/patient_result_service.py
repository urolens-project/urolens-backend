"""Patient-portal result listing/detail — Supabase-REST implementation,
deliberately left as-is (not ported to SQLAlchemy) per the consolidation
plan's deferred-services list."""
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.schemas.patient_portal import (
    PARTICLE_LABELS,
    ParticleCount,
    PatientResultDetailResponse,
    PatientResultItem,
)


class PatientResultService:
    """Read-side operations for the patient portal's result list/detail views."""

    def __init__(self, db: AsyncClient, audit_logger: AuditLogger):
        self.db = db
        self.audit_logger = audit_logger

    async def _resolve_patient_id(self, user_id: UUID) -> UUID:
        # Maps an authenticated portal user_id to their patient_id.
        # Raises HTTPException 404 (PATIENT_NOT_FOUND) if no patient row exists for this user.
        result = (
            await self.db.table("patients")
            .select("patient_id")
            .eq("user_id", str(user_id))
            .maybe_single()
            .execute()
        )
        row = result.data
        if not row:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )
            exc.error_code = "PATIENT_NOT_FOUND"
            raise exc
        return UUID(row["patient_id"])

    async def get_patient_results(self, user_id: UUID) -> list[PatientResultItem]:
        """List the authenticated patient's analysis results, newest-released first.

        Returns:
            One `PatientResultItem` per result, ordered by `released_at` descending.

        Raises:
            HTTPException: 404 (`PATIENT_NOT_FOUND`), if no patient record is
                linked to this user account.
        """
        patient_id = await self._resolve_patient_id(user_id)

        result = await (
            self.db.table("analysis_results")
            .select("result_id, status, released_at")
            .eq("patient_id", str(patient_id))
            .order("released_at", desc=True)
            .execute()
        )
        rows = result.data or []

        return [
            PatientResultItem(
                result_id=UUID(row["result_id"]),
                test_type="Urinalysis",
                status=row["status"],
                released_at=_parse_datetime(row.get("released_at")),
            )
            for row in rows
        ]

    async def get_result_detail(
        self, result_id: UUID, user_id: UUID, request: Request
    ) -> PatientResultDetailResponse:
        """Fetch one result's full detail for the patient portal, recording
        the view (a `result_views` row plus a `RESULT_VIEWED` audit entry).

        Args:
            user_id: the authenticated portal user; the result must belong to
                this user's own patient record.

        Returns:
            The result detail, including particle counts normalised against
            the fixed `PARTICLE_LABELS` set.

        Raises:
            HTTPException: 404 (`PATIENT_NOT_FOUND`), if no patient record is
                linked to this user account. 404 (`RESULT_NOT_FOUND`), if
                `result_id` doesn't exist. 403 (`ACCESS_DENIED`), if the
                result belongs to a different patient.
        """
        patient_id = await self._resolve_patient_id(user_id)

        result = await (
            self.db.table("analysis_results")
            .select("*")
            .eq("result_id", str(result_id))
            .maybe_single()
            .execute()
        )
        row = result.data

        if not row:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Result not found.",
            )
            exc.error_code = "RESULT_NOT_FOUND"
            raise exc

        row_patient_id = row.get("patient_id")
        if not row_patient_id or str(patient_id) != str(row_patient_id):
            exc = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied.",
            )
            exc.error_code = "ACCESS_DENIED"
            raise exc

        await self.db.table("result_views").insert({
            "result_id": str(result_id),
            "patient_id": str(patient_id),
        }).execute()

        await self.audit_logger.record(
            event_type="RESULT_VIEWED",
            entity_type="analysis_result",
            entity_id=row["result_id"],
            user_id=user_id,
            request=request,
        )

        # Normalise ai_findings → exactly 10 ParticleCount rows
        raw_findings: dict = row.get("ai_findings") or {}
        particle_counts = [
            ParticleCount(label=label, count=int(raw_findings.get(label, 0)))
            for label in PARTICLE_LABELS
        ]

        # Extract particle_classes — stored as JSONB (dict or list)
        raw_classes = row.get("particle_classes") or {}
        if isinstance(raw_classes, list):
            particle_classes = [str(c) for c in raw_classes]
        elif isinstance(raw_classes, dict):
            particle_classes = list(raw_classes.keys())
        else:
            particle_classes = []

        return PatientResultDetailResponse(
            status=row["status"],
            confirmed_at=_parse_datetime(row.get("confirmed_at")),
            confirmation_notes=row.get("interpretation"),
            analyzed_by=row.get("medtech_name"),
            particle_counts=particle_counts,
            particle_classes=particle_classes,
            smart_diagnosis_unavailable=bool(row.get("smart_diagnosis_unavailable", False)),
            test_type="Urinalysis",
            released_at=_parse_datetime(row.get("released_at")),
        )


def _parse_datetime(value: str | None) -> datetime | None:
    # Parses an ISO timestamp (with trailing "Z" normalised to "+00:00");
    # returns None for a missing or unparseable value rather than raising.
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
