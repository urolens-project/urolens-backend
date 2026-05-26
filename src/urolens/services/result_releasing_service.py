from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.encryption import decrypt_pii
from src.urolens.schemas.result_releasing import (
    ApprovedResultItem,
    ApprovedResultsResponse,
    PaginationMeta,
    ResultReleaseResponse,
)
from src.urolens.services.notification_service import NotificationService


class ResultReleasingService:
    def __init__(
        self,
        db: AsyncClient,
        audit_logger: AuditLogger,
        notification_service: NotificationService,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger
        self.notification_service = notification_service

    async def get_approved_results(
        self,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ApprovedResultsResponse:
        query = (
            self.db.table("analysis_results")
            .select("result_id, specimen_id, patient_id, updated_at")
            .eq("status", "APPROVED")
            .order("updated_at", desc=True)
            .limit(limit + 1)
        )
        if cursor:
            query = query.lt("updated_at", cursor)

        result = await query.execute()
        rows = result.data or []

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        items: list[ApprovedResultItem] = []
        for row in rows:
            patient_name = "Unknown Patient"
            sample_uid: str | None = None
            test_type: str | None = None

            if row.get("patient_id"):
                p_res = await self.db.table("patients").select(
                    "first_name, last_name"
                ).eq("patient_id", row["patient_id"]).execute()
                if p_res.data:
                    p = p_res.data[0]
                    try:
                        first = decrypt_pii(p["first_name"])
                        last = decrypt_pii(p["last_name"])
                        patient_name = f"{first} {last}"
                    except Exception:
                        patient_name = "Unknown Patient"

            if row.get("specimen_id"):
                s_res = await self.db.table("specimens").select(
                    "sample_uid, test_type"
                ).eq("specimen_id", row["specimen_id"]).execute()
                if s_res.data:
                    sample_uid = s_res.data[0].get("sample_uid")
                    test_type = s_res.data[0].get("test_type")

            items.append(
                ApprovedResultItem(
                    result_id=UUID(str(row["result_id"])),
                    patient_name=patient_name,
                    sample_uid=sample_uid,
                    test_type=test_type,
                    approved_at=row["updated_at"],
                )
            )

        next_cursor = rows[-1]["updated_at"] if has_more and rows else None
        return ApprovedResultsResponse(
            data=items,
            pagination=PaginationMeta(next_cursor=next_cursor, has_more=has_more),
        )

    async def release_result(
        self,
        result_id: UUID,
        release_method: str,
        current_user: dict,
        request: Request,
    ) -> ResultReleaseResponse:
        # Verify result exists
        r_res = await self.db.table("analysis_results").select(
            "result_id, status, patient_id, specimen_id"
        ).eq("result_id", str(result_id)).execute()

        if not r_res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Result not found.",
                        "details": {},
                    }
                },
            )

        row = r_res.data[0]

        if row["status"] != "APPROVED":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "RESULT_NOT_APPROVED",
                        "message": "Result is not in APPROVED status.",
                        "details": {},
                    }
                },
            )

        existing = await self.db.table("result_releases").select(
            "release_id"
        ).eq("result_id", str(result_id)).execute()
        if existing.data:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "ALREADY_RELEASED",
                        "message": "Result has already been released.",
                        "details": {},
                    }
                },
            )

        release_insert = await self.db.table("result_releases").insert({
            "result_id": str(result_id),
            "released_by": str(current_user["user_id"]),
            "release_method": release_method,
        }).execute()
        if not release_insert.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": {
                        "code": "RELEASE_FAILED",
                        "message": "Failed to create release record.",
                        "details": {},
                    }
                },
            )

        release_row = release_insert.data[0]
        release_id = release_row["release_id"]

        try:
            update_res = await self.db.table("analysis_results").update({
                "status": "RELEASED",
                "released_at": datetime.now(timezone.utc).isoformat(),
            }).eq("result_id", str(result_id)).execute()

            if not update_res.data:
                await self.db.table("result_releases").delete().eq(
                    "release_id", release_id
                ).execute()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": {
                            "code": "STATUS_UPDATE_FAILED",
                            "message": "Failed to update result status.",
                            "details": {},
                        }
                    },
                )
        except HTTPException:
            raise
        except Exception:
            await self.db.table("result_releases").delete().eq(
                "release_id", release_id
            ).execute()
            raise

        if release_method == "DIGITAL":
            patient_id = row.get("patient_id")
            if patient_id:
                patient_res = await self.db.table("patients").select("user_id").eq(
                    "patient_id", str(patient_id)
                ).execute()
                if patient_res.data and patient_res.data[0].get("user_id"):
                    await self.notification_service.notify(
                        UUID(str(patient_res.data[0]["user_id"])),
                        "Your lab result is now available.",
                        "RESULT_RELEASED",
                        entity_id=result_id,
                    )

            specimen_id = row.get("specimen_id")
            if specimen_id:
                spec_res = await self.db.table("specimens").select(
                    "lab_request_id"
                ).eq("specimen_id", str(specimen_id)).execute()
                if spec_res.data and spec_res.data[0].get("lab_request_id"):
                    lr_res = await self.db.table("lab_requests").select(
                        "physician_id"
                    ).eq(
                        "lab_request_id", str(spec_res.data[0]["lab_request_id"])
                    ).execute()
                    if lr_res.data and lr_res.data[0].get("physician_id"):
                        await self.notification_service.notify(
                            UUID(str(lr_res.data[0]["physician_id"])),
                            "A lab result has been released for your patient.",
                            "RESULT_RELEASED",
                            entity_id=result_id,
                        )

        await self.audit_logger.record(
            "RESULT_RELEASED",
            entity_type="result_release",
            entity_id=release_id,
            user_id=current_user["user_id"],
            detail_json={"result_id": str(result_id), "release_method": release_method},
            request=request,
        )

        return ResultReleaseResponse(
            release_id=UUID(str(release_id)),
            result_id=result_id,
            released_by=UUID(str(current_user["user_id"])),
            release_method=release_method,
            released_at=release_row.get("released_at", datetime.now(timezone.utc)),
        )
