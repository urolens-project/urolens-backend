"""MedTech queue management: workload views and specimen-to-MedTech
assignment, for both the supervisor/admin and receptionist-facing flows.
"""
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from src.core.audit_logger import AuditLogger
from src.core.encryption import decryptPii
from src.core.enums import UserRole
from src.core.exceptions import (
    NotFoundException,
    SpecimenNotFoundError,
    UnprocessableException,
)
from src.models.queue_assignment import QueueAssignment
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.queue import (
    MedTechWorkload,
    MedTechWorkloadItem,
    PendingSpecimenItem,
    QueueAssignRequest,
    QueueAssignResponse,
)
from src.services.notification_service import NotificationService

_ACTIVE_WORK_STATUSES = ("ASSIGNED", "IN_QUEUE", "PROCESSING")
"""The one workload definition shared by `getWorkloads` and
`getReceptionistWorkloads` (see `_medtechActiveCounts`) — specimen-status
based, not a `queue_assignments.status` count, since a specimen's own status
reflects real in-flight work regardless of any assignment-row bookkeeping.
"""


class QueueService:
    """Specimen queue assignment and workload reporting for MedTechs."""

    def __init__(
        self,
        db: AsyncClient,
        auditLogger: AuditLogger,
        _notificationService: NotificationService,
        sqlalchemyDb: AsyncSession,
    ):
        self.db = db
        self.auditLogger = auditLogger
        self._notificationService = _notificationService
        self.sqlalchemyDb = sqlalchemyDb

    async def _medtechActiveCounts(self) -> list[tuple[UUID, str, int]]:
        """One `(userId, username, activeCount)` row per active MedTech, in a
        single joined/aggregated query — not fetch-everything-then-filter-in-
        Python.

        `activeCount` is `Specimen.status IN _ACTIVE_WORK_STATUSES` for
        specimens currently assigned to that MedTech (`Specimen.medtechId`),
        not a `queue_assignments`-row count. `getWorkloads` and
        `getReceptionistWorkloads` both call this exact method, so they
        cannot disagree for the same data.
        """
        stmt = (
            select(User.userId, User.username, func.count(Specimen.specimenId))
            .outerjoin(
                Specimen,
                (Specimen.medtechId == User.userId)
                & (Specimen.status.in_(_ACTIVE_WORK_STATUSES)),
            )
            .where(User.role == UserRole.MEDTECH, User.isActive.is_(True))
            .group_by(User.userId, User.username)
        )
        rows = (await self.sqlalchemyDb.execute(stmt)).all()
        return list(rows)

    async def getWorkloads(self) -> list[MedTechWorkload]:
        """List active MedTechs with their current active-specimen count,
        ascending by count (least-loaded first).

        Returns:
            One `MedTechWorkload` per active MedTech.
        """
        rows = await self._medtechActiveCounts()
        workloads = [
            MedTechWorkload(medtechId=userId, username=username, queueCount=count)
            for userId, username, count in rows
        ]
        workloads.sort(key=lambda w: w.queueCount)
        return workloads

    async def assignSpecimen(
        self,
        data: QueueAssignRequest,
        assignedBy: UUID,
        request: Request,
    ) -> QueueAssignResponse:
        """Assign a `LABELED` specimen to an active MedTech, advancing it to
        `ASSIGNED`, notifying the MedTech, and writing an audit log entry.

        Race-safety (UROLENS-142): two concurrent requests for the same
        specimen can both pass the initial status/existing-assignment checks
        before either writes — closed at the DB level, not just by that
        pre-check, via a partial unique index (`ix_queue_assignments_one_
        active_per_specimen`, migration 0035) on `specimen_id WHERE status =
        'ACTIVE'`, plus a conditional `UPDATE ... WHERE status = 'LABELED'`
        on the specimen row. The insert runs inside a SAVEPOINT
        (`db.begin_nested()`) so a constraint violation only rolls back that
        one statement, not the whole transaction — this is exactly the
        transactional guarantee AsyncSession has over the old Supabase-REST
        multi-step flow (three independent HTTP calls, no shared transaction).
        Whichever side loses — constraint violation, or the conditional
        update affecting 0 rows — gets the same `SPECIMEN_ALREADY_ASSIGNED`
        the pre-check would already raise in the non-race case.

        Args:
            assigned_by: the authenticated user recorded as the assignment's author.

        Returns:
            Confirmation of the created assignment.

        Raises:
            SpecimenNotFoundError: `data.specimen_id` doesn't exist.
            UnprocessableException: `INVALID_SPECIMEN_STATUS`, if the
                specimen isn't `LABELED`. `SPECIMEN_ALREADY_ASSIGNED`, if it
                already has (or, under a race, ends up with) an active
                assignment.
            NotFoundException: `MEDTECH_NOT_FOUND`, if `data.medtech_id`
                doesn't exist, isn't `MEDTECH`, or isn't active.
        """
        db = self.sqlalchemyDb

        specimen = await db.get(Specimen, data.specimenId)
        if specimen is None:
            raise SpecimenNotFoundError(str(data.specimenId))
        if specimen.status != "LABELED":
            raise UnprocessableException(
                code="INVALID_SPECIMEN_STATUS",
                message="Specimen must be in LABELED status to be assigned.",
            )

        medtech = await db.get(User, data.medtechId)
        if medtech is None or medtech.role != UserRole.MEDTECH or not medtech.isActive:
            raise NotFoundException(
                code="MEDTECH_NOT_FOUND", message="MedTech not found or not active."
            )

        existingActive = (
            await db.execute(
                select(QueueAssignment.assignmentId).where(
                    QueueAssignment.specimenId == data.specimenId,
                    QueueAssignment.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if existingActive is not None:
            raise UnprocessableException(
                code="SPECIMEN_ALREADY_ASSIGNED",
                message="This specimen is already assigned to a MedTech.",
            )

        assignedAt = datetime.now(UTC)
        assignment = QueueAssignment(
            specimenId=data.specimenId,
            medtechId=data.medtechId,
            assignedBy=assignedBy,
            assignedAt=assignedAt,
            status="ACTIVE",
        )
        db.add(assignment)
        try:
            async with db.begin_nested():
                await db.flush([assignment])
        except IntegrityError:
            # Lost the race: the other request's insert won the partial
            # unique index first. Same code the pre-check above would have
            # raised had it run a moment later.
            raise UnprocessableException(
                code="SPECIMEN_ALREADY_ASSIGNED",
                message="This specimen is already assigned to a MedTech.",
            ) from None

        updateResult = await db.execute(
            update(Specimen)
            .where(Specimen.specimenId == data.specimenId, Specimen.status == "LABELED")
            .values(status="ASSIGNED", medtechId=data.medtechId, assignedAt=assignedAt)
        )
        if updateResult.rowcount == 0:
            # The specimen's status changed out from under us between our
            # first read and this UPDATE (e.g. lost a different race on the
            # same specimen) — same outcome as already-assigned, not a
            # separate error path.
            await db.rollback()
            raise UnprocessableException(
                code="SPECIMEN_ALREADY_ASSIGNED",
                message="This specimen is already assigned to a MedTech.",
            )

        await self._notificationService.notify(
            data.medtechId,
            f"New specimen assigned: {specimen.sampleUid or data.specimenId}",
            "SAMPLE_ASSIGNED",
            entityId=data.specimenId,
        )

        await self.auditLogger.record(
            "QUEUE_ASSIGNED",
            entityType="queue_assignment",
            entityId=assignment.assignmentId,
            userId=assignedBy,
            detailJson={
                "specimen_id": str(data.specimenId),
                "medtech_id": str(data.medtechId),
            },
            request=request,
            db=db,
        )

        await db.commit()

        return QueueAssignResponse(
            assignmentId=assignment.assignmentId,
            specimenId=data.specimenId,
            medtechId=data.medtechId,
            assignedBy=assignedBy,
            assignedAt=assignedAt,
            status="ACTIVE",
        )

    # ── Receptionist-facing methods (STORY-WEB-08) ────────────────────────────

    async def getPendingSpecimens(self) -> list[PendingSpecimenItem]:
        """Return all LABELED specimens with decrypted patient PII and test type.
        Ordered by received_at ascending (FIFO).

        Returns:
            One `PendingSpecimenItem` per `LABELED` specimen. A row whose
            patient name fails to decrypt falls back to `"Unknown Patient"`
            rather than being excluded.
        """
        specRes = await self.db.table("specimens").select(
            "specimen_id, sample_uid, status, received_at, lab_request_id"
        ).eq("status", "LABELED").order("received_at", desc=False).execute()

        specimens = specRes.data or []
        if not specimens:
            return []

        labRequestIds = list({str(s["lab_request_id"]) for s in specimens if s.get("lab_request_id")})
        lrRes = await self.db.table("lab_requests").select(
            "lab_request_id, test_type, patient_id"
        ).in_("lab_request_id", labRequestIds).execute()

        lrMap = {str(lr["lab_request_id"]): lr for lr in (lrRes.data or [])}

        patientIds = list({str(lr["patient_id"]) for lr in (lrRes.data or []) if lr.get("patient_id")})
        patMap: dict[str, dict] = {}
        if patientIds:
            patRes = await self.db.table("patients").select(
                "patient_id, first_name, last_name"
            ).in_("patient_id", patientIds).execute()
            patMap = {str(p["patient_id"]): p for p in (patRes.data or [])}

        items: list[PendingSpecimenItem] = []
        for spec in specimens:
            lr = lrMap.get(str(spec.get("lab_request_id", "")), {})
            pat = patMap.get(str(lr.get("patient_id", "")), {})

            try:
                first = decryptPii(pat["first_name"]) if pat.get("first_name") else ""
                last = decryptPii(pat["last_name"]) if pat.get("last_name") else ""
            except Exception:
                first = last = ""
            patientName = f"{first} {last}".strip() or "Unknown Patient"

            items.append(PendingSpecimenItem(
                specimenId=UUID(spec["specimen_id"]),
                sampleUid=spec.get("sample_uid") or spec["specimen_id"][:8].upper(),
                patientName=patientName,
                testType=lr.get("test_type") or "—",
                receivedAt=spec["received_at"],
                status=spec["status"],
            ))

        return items

    async def getReceptionistWorkloads(self) -> list[MedTechWorkloadItem]:
        """Return all active MedTechs with their active specimen queue depth,
        sorted ascending by `activeCount` (least-loaded first).

        `fullName` is the username — `users` has no display-name column
        (confirmed absent again here; same data gap 137 already found, not
        fabricated). Uses the same `_medtechActiveCounts` query `getWorkloads`
        does, so the two can never report different numbers for the same
        MedTech under the same data.

        Returns:
            One `MedTechWorkloadItem` per active MedTech.
        """
        rows = await self._medtechActiveCounts()
        items = [
            MedTechWorkloadItem(userId=userId, fullName=username, activeCount=count)
            for userId, username, count in rows
        ]
        items.sort(key=lambda x: x.activeCount)
        return items
