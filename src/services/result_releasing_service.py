"""Supervisor result release — SQLAlchemy `AsyncSession` implementation.
Lists `APPROVED` results awaiting release and performs the release
transaction (status update + notifications + audit log) in one committed
unit of work.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.encryption import decryptPii
from src.core.exceptions import NotFoundException, UnprocessableException
from src.models.analysis_result import AnalysisResult
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.models.result_release import ResultRelease
from src.models.specimen import Specimen
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
        db: AsyncSession,
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
        stmt = (
            select(AnalysisResult)
            .where(AnalysisResult.status == "APPROVED")
            .order_by(AnalysisResult.updatedAt.desc())
            .limit(limit + 1)
        )
        if cursor:
            cursorDt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            stmt = stmt.where(AnalysisResult.updatedAt < cursorDt)

        rows = (await self.db.execute(stmt)).scalars().all()

        hasMore = len(rows) > limit
        if hasMore:
            rows = rows[:limit]

        items: list[ApprovedResultItem] = []
        for row in rows:
            patientName = "Unknown Patient"
            sampleUid: str | None = None
            testType: str | None = None

            if row.patientId:
                patient = await self.db.get(Patient, row.patientId)
                if patient is not None:
                    try:
                        first = decryptPii(patient.firstName)
                        last = decryptPii(patient.lastName)
                        patientName = f"{first} {last}"
                    except Exception:
                        patientName = "Unknown Patient"

            if row.specimenId:
                specimen = await self.db.get(Specimen, row.specimenId)
                if specimen is not None:
                    sampleUid = specimen.sampleUid
                    testType = specimen.testType

            items.append(
                ApprovedResultItem(
                    resultId=row.resultId,
                    patientName=patientName,
                    sampleUid=sampleUid,
                    testType=testType,
                    approvedAt=row.updatedAt,
                )
            )

        nextCursor = rows[-1].updatedAt.isoformat() if hasMore and rows else None
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
        entry — all in one transaction, committed once. Nothing is written
        unless every step succeeds; on any failure the whole unit of work is
        rolled back (by `getDb`'s dependency), so there's no manual
        compensating delete for the release row the way the prior
        Supabase-REST version needed (each of its calls auto-committed
        individually).

        Args:
            current_user: the authenticated caller; recorded as `released_by`.

        Returns:
            Confirmation of the release, including its generated `release_id`.

        Raises:
            HTTPException: 404 (`NOT_FOUND`), if `result_id` doesn't exist.
                422 (`RESULT_NOT_APPROVED`), if the result isn't in `APPROVED`
                status. 422 (`ALREADY_RELEASED`), if it's already been
                released.
        """
        row = await self.db.get(AnalysisResult, resultId)

        if row is None:
            raise NotFoundException(message="Result not found.")

        if row.status != "APPROVED":
            raise UnprocessableException(
                code="RESULT_NOT_APPROVED", message="Result is not in APPROVED status."
            )

        existing = await self.db.execute(
            select(ResultRelease.releaseId).where(ResultRelease.resultId == resultId)
        )
        if existing.scalar_one_or_none() is not None:
            raise UnprocessableException(
                code="ALREADY_RELEASED", message="Result has already been released."
            )

        release = ResultRelease(
            resultId=resultId,
            releasedBy=UUID(str(currentUser["user_id"])),
            releaseMethod=releaseMethod,
        )
        self.db.add(release)
        await self.db.flush([release])

        nowUtc = datetime.now(UTC)
        row.status = "RELEASED"
        row.releasedAt = nowUtc

        specimen = await self.db.get(Specimen, row.specimenId)
        if specimen is not None:
            specimen.status = "COMPLETED"
            specimen.completedAt = nowUtc

        if releaseMethod == "DIGITAL":
            if row.patientId:
                patient = await self.db.get(Patient, row.patientId)
                if patient is not None and patient.userId:
                    await self._notificationService.notify(
                        patient.userId,
                        "Your lab result is now available.",
                        "RESULT_RELEASED",
                        entityId=resultId,
                    )

            if specimen is not None and specimen.labRequestId:
                labRequest = await self.db.get(LabRequest, specimen.labRequestId)
                if labRequest is not None and labRequest.physicianId:
                    await self._notificationService.notify(
                        labRequest.physicianId,
                        "A lab result has been released for your patient.",
                        "RESULT_RELEASED",
                        entityId=resultId,
                    )

        await self.auditLogger.record(
            "RESULT_RELEASED",
            entityType="result_release",
            entityId=release.releaseId,
            userId=currentUser["user_id"],
            detailJson={"result_id": str(resultId), "release_method": releaseMethod},
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(release)

        return ResultReleaseResponse(
            releaseId=release.releaseId,
            resultId=resultId,
            releasedBy=UUID(str(currentUser["user_id"])),
            releaseMethod=releaseMethod,
            releasedAt=release.releasedAt or nowUtc,
        )
