"""Patient-portal result listing/detail — Supabase-REST implementation,
deliberately left as-is (not ported to SQLAlchemy) per the consolidation
plan's deferred-services list.
"""
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

    def __init__(self, db: AsyncClient, auditLogger: AuditLogger):
        self.db = db
        self.auditLogger = auditLogger

    async def _resolvePatientId(self, userId: UUID) -> UUID:
        # Maps an authenticated portal user_id to their patient_id.
        # Raises HTTPException 404 (PATIENT_NOT_FOUND) if no patient row exists for this user.
        result = (
            await self.db.table("patients")
            .select("patient_id")
            .eq("user_id", str(userId))
            .maybe_single()
            .execute()
        )
        row = result.data
        if not row:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )
            exc.errorCode = "PATIENT_NOT_FOUND"
            raise exc
        return UUID(row["patient_id"])

    async def getPatientResults(self, userId: UUID) -> list[PatientResultItem]:
        """List the authenticated patient's analysis results, newest-released first.

        Returns:
            One `PatientResultItem` per result, ordered by `released_at` descending.

        Raises:
            HTTPException: 404 (`PATIENT_NOT_FOUND`), if no patient record is
                linked to this user account.
        """
        patientId = await self._resolvePatientId(userId)

        result = await (
            self.db.table("analysis_results")
            .select("result_id, status, released_at")
            .eq("patient_id", str(patientId))
            .order("released_at", desc=True)
            .execute()
        )
        rows = result.data or []

        return [
            PatientResultItem(
                resultId=UUID(row["result_id"]),
                testType="Urinalysis",
                status=row["status"],
                releasedAt=_parseDatetime(row.get("released_at")),
            )
            for row in rows
        ]

    async def getResultDetail(
        self, resultId: UUID, userId: UUID, request: Request
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
        patientId = await self._resolvePatientId(userId)

        result = await (
            self.db.table("analysis_results")
            .select("*")
            .eq("result_id", str(resultId))
            .maybe_single()
            .execute()
        )
        row = result.data

        if not row:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Result not found.",
            )
            exc.errorCode = "RESULT_NOT_FOUND"
            raise exc

        rowPatientId = row.get("patient_id")
        if not rowPatientId or str(patientId) != str(rowPatientId):
            exc = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied.",
            )
            exc.errorCode = "ACCESS_DENIED"
            raise exc

        await self.db.table("result_views").insert({
            "result_id": str(resultId),
            "patient_id": str(patientId),
        }).execute()

        await self.auditLogger.record(
            eventType="RESULT_VIEWED",
            entityType="analysis_result",
            entityId=row["result_id"],
            userId=userId,
            request=request,
        )

        # Normalise ai_findings → exactly 10 ParticleCount rows
        rawFindings: dict = row.get("ai_findings") or {}
        particleCounts = [
            ParticleCount(label=label, count=int(rawFindings.get(label, 0)))
            for label in PARTICLE_LABELS
        ]

        # Extract particle_classes — stored as JSONB (dict or list)
        rawClasses = row.get("particle_classes") or {}
        if isinstance(rawClasses, list):
            particleClasses = [str(c) for c in rawClasses]
        elif isinstance(rawClasses, dict):
            particleClasses = list(rawClasses.keys())
        else:
            particleClasses = []

        return PatientResultDetailResponse(
            status=row["status"],
            confirmedAt=_parseDatetime(row.get("confirmed_at")),
            confirmationNotes=row.get("interpretation"),
            analyzedBy=row.get("medtech_name"),
            particleCounts=particleCounts,
            particleClasses=particleClasses,
            smartDiagnosisUnavailable=bool(row.get("smart_diagnosis_unavailable", False)),
            testType="Urinalysis",
            releasedAt=_parseDatetime(row.get("released_at")),
        )


def _parseDatetime(value: str | None) -> datetime | None:
    # Parses an ISO timestamp (with trailing "Z" normalised to "+00:00");
    # returns None for a missing or unparseable value rather than raising.
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
