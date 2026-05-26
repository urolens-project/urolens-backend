from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.schemas.patient_portal import (
    CellCounts,
    PatientResultDetail,
    PatientResultSummary,
)


class PatientResultService:
    def __init__(self, db: AsyncClient, audit_logger: AuditLogger):
        self.db = db
        self.audit_logger = audit_logger

    async def _resolve_patient_id(self, user_id: UUID) -> UUID:
        result = await self.db.table("patients").select("patient_id").eq("user_id", str(user_id)).maybe_single().execute()
        row = result.data
        if not row:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )
            exc.error_code = "PATIENT_NOT_FOUND"
            raise exc
        return UUID(row["patient_id"])

    async def get_patient_results(
        self, user_id: UUID
    ) -> list[PatientResultSummary]:
        patient_id = await self._resolve_patient_id(user_id)

        result = await (
            self.db.table("analysis_results")
            .select("*")
            .eq("patient_id", str(patient_id))
            .order("created_at", desc=True)
            .execute()
        )
        rows = result.data or []

        return [
            PatientResultSummary(
                result_id=UUID(row["result_id"]),
                specimen_id=UUID(row["specimen_id"]),
                status=row["status"],
                released_at=_parse_datetime(row.get("released_at")),
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    async def get_result_detail(
        self, result_id: UUID, user_id: UUID, request: Request
    ) -> PatientResultDetail:
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

        cell_counts = None
        if row.get("cell_counts"):
            cell_counts = CellCounts(**row["cell_counts"])

        return PatientResultDetail(
            result_id=UUID(row["result_id"]),
            specimen_id=UUID(row["specimen_id"]),
            patient_id=UUID(row["patient_id"]) if row.get("patient_id") else None,
            status=row["status"],
            cell_counts=cell_counts,
            interpretation=row.get("interpretation"),
            medtech_name=row.get("medtech_name"),
            pathologist_name=row.get("pathologist_name"),
            pathologist_license=row.get("pathologist_license"),
            confirmed_at=_parse_datetime(row.get("confirmed_at")),
            released_at=_parse_datetime(row.get("released_at")),
            created_at=_parse_datetime(row["created_at"]),
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
