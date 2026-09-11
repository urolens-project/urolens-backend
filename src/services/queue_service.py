"""MedTech queue management: workload views and specimen-to-MedTech
assignment, for both the supervisor/admin and receptionist-facing flows.
"""
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, Request, status
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
from src.schemas.queue import (
    MedTechWorkload,
    MedTechWorkloadItem,
    PendingSpecimenItem,
    QueueAssignRequest,
    QueueAssignResponse,
)
from src.services.notification_service import NotificationService


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

    async def getWorkloads(self) -> list[MedTechWorkload]:
        """List active MedTechs with their current active queue-assignment
        count, ascending by count (least-loaded first).

        Returns:
            One `MedTechWorkload` per active MedTech.
        """
        usersResult = await self.db.table("users").select(
            "user_id", "username"
        ).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

        medtechs = usersResult.data or []
        workloads: list[MedTechWorkload] = []

        for medtech in medtechs:
            medtechId = medtech["user_id"]
            queueResult = await self.db.table("queue_assignments").select(
                "assignment_id"
            ).eq("medtech_id", str(medtechId)).eq("status", "ACTIVE").execute()
            queueCount = len(queueResult.data or [])

            workloads.append(MedTechWorkload(
                medtechId=medtechId,
                username=medtech["username"],
                queueCount=queueCount,
            ))

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

        If the specimen-status update fails after the assignment row is
        created, the assignment row is deleted to avoid leaving an orphaned
        assignment with no corresponding status change.

        Args:
            assigned_by: the authenticated user recorded as the assignment's author.

        Returns:
            Confirmation of the created assignment.

        Raises:
            HTTPException: 404 (`SPECIMEN_NOT_FOUND`/`MEDTECH_NOT_FOUND`), if
                the specimen or MedTech doesn't exist (or the MedTech isn't
                active). 422 (`INVALID_SPECIMEN_STATUS`), if the specimen
                isn't `LABELED`. 422 (`SPECIMEN_ALREADY_ASSIGNED`), if it
                already has an active assignment. 500
                (`ASSIGNMENT_FAILED`/`STATUS_UPDATE_FAILED`), if the
                assignment insert or the specimen status update returns no data.
        """
        specimenResult = await self.db.table("specimens").select(
            "specimen_id", "status"
        ).eq("specimen_id", str(data.specimenId)).execute()

        if not specimenResult.data:
            raise SpecimenNotFoundError(str(data.specimenId))

        specimen = specimenResult.data[0]
        if specimen["status"] != "LABELED":
            raise UnprocessableException(
                code="INVALID_SPECIMEN_STATUS",
                message="Specimen must be in LABELED status to be assigned.",
            )

        medtechResult = await self.db.table("users").select(
            "user_id"
        ).eq("user_id", str(data.medtechId)).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

        if not medtechResult.data:
            raise NotFoundException(
                code="MEDTECH_NOT_FOUND", message="MedTech not found or not active."
            )

        existingAssignment = await self.db.table("queue_assignments").select(
            "assignment_id"
        ).eq("specimen_id", str(data.specimenId)).eq("status", "ACTIVE").execute()

        if existingAssignment.data:
            raise UnprocessableException(
                code="SPECIMEN_ALREADY_ASSIGNED",
                message="This specimen is already assigned to a MedTech.",
            )

        assignmentPayload = {
            "specimen_id": str(data.specimenId),
            "medtech_id": str(data.medtechId),
            "assigned_by": str(assignedBy),
            "assigned_at": datetime.now(UTC).isoformat(),
            "status": "ACTIVE",
        }

        assignmentResult = await self.db.table("queue_assignments").insert(assignmentPayload).execute()
        if not assignmentResult.data:
            exc = HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create queue assignment.",
            )
            exc.errorCode = "ASSIGNMENT_FAILED"
            raise exc

        assignmentRow = assignmentResult.data[0]
        assignmentId = assignmentRow["assignment_id"]

        try:
            updateResult = await self.db.table("specimens").update(
            {
                "status": "ASSIGNED",
                "medtech_id": str(data.medtechId),  # ← add this
                "assigned_at": datetime.now(UTC).isoformat(),  # ← good to track too
            }
             ).eq("specimen_id", str(data.specimenId)).execute()

            if not updateResult.data:
                await self.db.table("queue_assignments").delete().eq(
                    "assignment_id", assignmentId
                ).execute()
                exc = HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to update specimen status.",
                )
                exc.errorCode = "STATUS_UPDATE_FAILED"
                raise exc
        except HTTPException:
            raise
        except Exception:
            await self.db.table("queue_assignments").delete().eq(
                "assignment_id", assignmentId
            ).execute()
            raise

        await self._notificationService.notify(
            data.medtechId,
            f"New specimen assigned: {data.specimenId}",
            "SAMPLE_ASSIGNED",
            entityId=data.specimenId,
        )

        await self.auditLogger.record(
            "QUEUE_ASSIGNED",
            entityType="queue_assignment",
            entityId=assignmentId,
            userId=assignedBy,
            db=self.sqlalchemyDb,
            detailJson={
                "specimen_id": str(data.specimenId),
                "medtech_id": str(data.medtechId),
            },
            request=request,
        )

        return QueueAssignResponse(
            assignmentId=assignmentId,
            specimenId=data.specimenId,
            medtechId=data.medtechId,
            assignedBy=assignedBy,
            assignedAt=assignmentRow.get("assigned_at", datetime.now(UTC)),
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
        """Return all active MedTechs with their active specimen queue depth.
        Active = specimens.status IN (ASSIGNED, IN_QUEUE, PROCESSING).
        Sorted ascending by active_count (least-loaded first).

        Returns:
            One `MedTechWorkloadItem` per active MedTech.
        """
        usersRes = await self.db.table("users").select(
            "user_id, username"
        ).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

        medtechs = usersRes.data or []
        if not medtechs:
            return []

        medtechIds = [str(m["user_id"]) for m in medtechs]

        qaRes = await self.db.table("queue_assignments").select(
            "medtech_id, specimen_id"
        ).in_("medtech_id", medtechIds).execute()

        qaRows = qaRes.data or []
        activeSpecIds: set[str] = set()

        if qaRows:
            specIds = list({str(qa["specimen_id"]) for qa in qaRows})
            specRes = await self.db.table("specimens").select(
                "specimen_id"
            ).in_("specimen_id", specIds).in_(
                "status", ["ASSIGNED", "IN_QUEUE", "PROCESSING"]
            ).execute()
            activeSpecIds = {str(s["specimen_id"]) for s in (specRes.data or [])}

        medtechCounts: dict[str, int] = {m["user_id"]: 0 for m in medtechs}
        for qa in qaRows:
            if str(qa["specimen_id"]) in activeSpecIds:
                mid = str(qa["medtech_id"])
                medtechCounts[mid] = medtechCounts.get(mid, 0) + 1

        items = [
            MedTechWorkloadItem(
                userId=UUID(m["user_id"]),
                fullName=m["username"],
                activeCount=medtechCounts.get(m["user_id"], 0),
            )
            for m in medtechs
        ]
        items.sort(key=lambda x: x.activeCount)
        return items
