"""Supervisor result release — Supabase-REST implementation, deliberately
left as-is (not ported to SQLAlchemy) per the consolidation plan's
deferred-services list.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.core.audit_logger import AuditLogger
from src.core.encryption import decryptPii
from src.schemas.result_releasing import (
    ApprovedResultItem,
    ApprovedResultsResponse,
    PaginationMeta,
    ResultReleaseResponse,
)
from src.services.notification_service import NotificationService


class ResultReleasingService:
    """Lists approved results awaiting release and performs the release
    transaction (status update + notifications + audit log).
    """

    def __init__(
        self,
        db: AsyncClient,
        auditLogger: AuditLogger,
        _notificationService: NotificationService,
    ) -> None:
        self.db = db
        self.auditLogger = auditLogger
        self._notificationService = _notificationService

    async def getApprovedResults(
        self,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ApprovedResultsResponse:
        """List `APPROVED` results awaiting release, newest-updated first,
        cursor-paginated.

        Args:
            limit: max rows to return per page.
            cursor: an `updated_at` value from a previous page's
                `next_cursor`; rows with `updated_at` before it are returned.
                `None` starts from the most recent.

        Returns:
            A page of `ApprovedResultItem`s (patient name decrypted, or
            `"Unknown Patient"` if decryption fails) plus pagination metadata.
        """
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

        hasMore = len(rows) > limit
        if hasMore:
            rows = rows[:limit]

        items: list[ApprovedResultItem] = []
        for row in rows:
            patientName = "Unknown Patient"
            sampleUid: str | None = None
            testType: str | None = None

            if row.get("patient_id"):
                pRes = await self.db.table("patients").select(
                    "first_name, last_name"
                ).eq("patient_id", row["patient_id"]).execute()
                if pRes.data:
                    p = pRes.data[0]
                    try:
                        first = decryptPii(p["first_name"])
                        last = decryptPii(p["last_name"])
                        patientName = f"{first} {last}"
                    except Exception:
                        patientName = "Unknown Patient"

            if row.get("specimen_id"):
                sRes = await self.db.table("specimens").select(
                    "sample_uid, test_type"
                ).eq("specimen_id", row["specimen_id"]).execute()
                if sRes.data:
                    sampleUid = sRes.data[0].get("sample_uid")
                    testType = sRes.data[0].get("test_type")

            items.append(
                ApprovedResultItem(
                    resultId=UUID(str(row["result_id"])),
                    patientName=patientName,
                    sampleUid=sampleUid,
                    testType=testType,
                    approvedAt=row["updated_at"],
                )
            )

        nextCursor = rows[-1]["updated_at"] if hasMore and rows else None
        return ApprovedResultsResponse(
            data=items,
            pagination=PaginationMeta(nextCursor=nextCursor, hasMore=hasMore),
        )

    async def releaseResult(
        self,
        resultId: UUID,
        releaseMethod: str,
        currentUser: dict,
        request: Request,
    ) -> ResultReleaseResponse:
        """Release an `APPROVED` result: creates the `result_releases` row,
        transitions the result to `RELEASED` and its specimen to
        `COMPLETED`, notifies the patient (if `release_method` is
        `"DIGITAL"`) and the ordering physician, and writes an audit log
        entry.

        If the status update after creating the release record fails, the
        release record is deleted to avoid leaving an orphaned release with
        no corresponding status change.

        Args:
            current_user: the authenticated caller; recorded as `released_by`.

        Returns:
            Confirmation of the release, including its generated `release_id`.

        Raises:
            HTTPException: 404 (`NOT_FOUND`), if `result_id` doesn't exist.
                422 (`RESULT_NOT_APPROVED`), if the result isn't in `APPROVED`
                status. 422 (`ALREADY_RELEASED`), if it's already been
                released. 500 (`RELEASE_FAILED`/`STATUS_UPDATE_FAILED`), if
                the release-record insert or the subsequent status update
                returns no data.
        """
        # Verify result exists
        rRes = await self.db.table("analysis_results").select(
            "result_id, status, patient_id, specimen_id"
        ).eq("result_id", str(resultId)).execute()

        if not rRes.data:
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

        row = rRes.data[0]

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
        ).eq("result_id", str(resultId)).execute()
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

        releaseInsert = await self.db.table("result_releases").insert({
            "result_id": str(resultId),
            "released_by": str(currentUser["user_id"]),
            "release_method": releaseMethod,
        }).execute()
        if not releaseInsert.data:
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

        releaseRow = releaseInsert.data[0]
        releaseId = releaseRow["release_id"]

        try:
            nowIso = datetime.now(UTC).isoformat()

            updateRes = await self.db.table("analysis_results").update({
                "status": "RELEASED",
                "released_at": nowIso,
            }).eq("result_id", str(resultId)).execute()

            if not updateRes.data:
                await self.db.table("result_releases").delete().eq(
                    "release_id", releaseId
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

            # Mark the specimen COMPLETED so it drops off the medtech's active queue
            await self.db.table("specimens").update({
                "status": "COMPLETED",
                "completed_at": nowIso,
            }).eq("specimen_id", str(row["specimen_id"])).execute()

        except HTTPException:
            raise
        except Exception:
            await self.db.table("result_releases").delete().eq(
                "release_id", releaseId
            ).execute()
            raise

        if releaseMethod == "DIGITAL":
            patientId = row.get("patient_id")
            if patientId:
                patientRes = await self.db.table("patients").select("user_id").eq(
                    "patient_id", str(patientId)
                ).execute()
                if patientRes.data and patientRes.data[0].get("user_id"):
                    await self._notificationService.notify(
                        UUID(str(patientRes.data[0]["user_id"])),
                        "Your lab result is now available.",
                        "RESULT_RELEASED",
                        entityId=resultId,
                    )

            specimenId = row.get("specimen_id")
            if specimenId:
                specRes = await self.db.table("specimens").select(
                    "lab_request_id"
                ).eq("specimen_id", str(specimenId)).execute()
                if specRes.data and specRes.data[0].get("lab_request_id"):
                    lrRes = await self.db.table("lab_requests").select(
                        "physician_id"
                    ).eq(
                        "lab_request_id", str(specRes.data[0]["lab_request_id"])
                    ).execute()
                    if lrRes.data and lrRes.data[0].get("physician_id"):
                        await self._notificationService.notify(
                            UUID(str(lrRes.data[0]["physician_id"])),
                            "A lab result has been released for your patient.",
                            "RESULT_RELEASED",
                            entityId=resultId,
                        )

        await self.auditLogger.record(
            "RESULT_RELEASED",
            entityType="result_release",
            entityId=releaseId,
            userId=currentUser["user_id"],
            detailJson={"result_id": str(resultId), "release_method": releaseMethod},
            request=request,
        )

        return ResultReleaseResponse(
            releaseId=UUID(str(releaseId)),
            resultId=resultId,
            releasedBy=UUID(str(currentUser["user_id"])),
            releaseMethod=releaseMethod,
            releasedAt=releaseRow.get("released_at", datetime.now(UTC)),
        )
