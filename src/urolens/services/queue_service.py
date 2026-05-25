from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.schemas.queue import MedTechWorkload, QueueAssignRequest, QueueAssignResponse
from src.urolens.services.notification_service import NotificationService


class QueueService:
    def __init__(
        self,
        db: AsyncClient,
        audit_logger: AuditLogger,
        notification_service: NotificationService,
    ):
        self.db = db
        self.audit_logger = audit_logger
        self.notification_service = notification_service

    async def get_workloads(self) -> list[MedTechWorkload]:
        users_result = await self.db.table("users").select(
            "user_id", "username"
        ).eq("role", "medtech").eq("is_active", True).execute()

        medtechs = users_result.data or []
        workloads: list[MedTechWorkload] = []

        for medtech in medtechs:
            medtech_id = medtech["user_id"]
            queue_result = await self.db.table("queue_assignments").select(
                "assignment_id"
            ).eq("medtech_id", str(medtech_id)).eq("status", "ACTIVE").execute()
            queue_count = len(queue_result.data or [])

            workloads.append(MedTechWorkload(
                medtech_id=medtech_id,
                username=medtech["username"],
                queue_count=queue_count,
            ))

        workloads.sort(key=lambda w: w.queue_count)
        return workloads

    async def assign_specimen(
        self,
        data: QueueAssignRequest,
        assigned_by: UUID,
        request: Request,
    ) -> QueueAssignResponse:
        specimen_result = await self.db.table("specimens").select(
            "specimen_id", "status"
        ).eq("specimen_id", str(data.specimen_id)).execute()

        if not specimen_result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "SPECIMEN_NOT_FOUND",
                        "message": "Specimen not found.",
                        "details": {},
                    }
                },
            )

        specimen = specimen_result.data[0]
        if specimen["status"] != "LABELED":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "INVALID_SPECIMEN_STATUS",
                        "message": "Specimen must be in LABELED status to be assigned.",
                        "details": {},
                    }
                },
            )

        medtech_result = await self.db.table("users").select(
            "user_id"
        ).eq("user_id", str(data.medtech_id)).eq("role", "medtech").eq("is_active", True).execute()

        if not medtech_result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "MEDTECH_NOT_FOUND",
                        "message": "MedTech not found or not active.",
                        "details": {},
                    }
                },
            )

        existing_assignment = await self.db.table("queue_assignments").select(
            "assignment_id"
        ).eq("specimen_id", str(data.specimen_id)).eq("status", "ACTIVE").execute()

        if existing_assignment.data:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "SPECIMEN_ALREADY_ASSIGNED",
                        "message": "This specimen is already assigned to a MedTech.",
                        "details": {},
                    }
                },
            )

        assignment_payload = {
            "specimen_id": str(data.specimen_id),
            "medtech_id": str(data.medtech_id),
            "assigned_by": str(assigned_by),
            "assigned_at": datetime.now(timezone.utc).isoformat(),
            "status": "ACTIVE",
        }

        assignment_result = await self.db.table("queue_assignments").insert(assignment_payload).execute()
        if not assignment_result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": {
                        "code": "ASSIGNMENT_FAILED",
                        "message": "Failed to create queue assignment.",
                        "details": {},
                    }
                },
            )

        assignment_row = assignment_result.data[0]
        assignment_id = assignment_row["assignment_id"]

        try:
            update_result = await self.db.table("specimens").update(
                {"status": "ASSIGNED"}
            ).eq("specimen_id", str(data.specimen_id)).execute()

            if not update_result.data:
                await self.db.table("queue_assignments").delete().eq(
                    "assignment_id", assignment_id
                ).execute()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": {
                            "code": "STATUS_UPDATE_FAILED",
                            "message": "Failed to update specimen status.",
                            "details": {},
                        }
                    },
                )
        except HTTPException:
            raise
        except Exception:
            await self.db.table("queue_assignments").delete().eq(
                "assignment_id", assignment_id
            ).execute()
            raise

        await self.notification_service.notify(
            data.medtech_id,
            f"New specimen assigned: {data.specimen_id}",
            "SAMPLE_ASSIGNED",
            entity_id=data.specimen_id,
        )

        await self.audit_logger.record(
            "QUEUE_ASSIGNED",
            entity_type="queue_assignment",
            entity_id=assignment_id,
            user_id=assigned_by,
            detail_json={
                "specimen_id": str(data.specimen_id),
                "medtech_id": str(data.medtech_id),
            },
            db=self.db,
            request=request,
        )

        return QueueAssignResponse(
            assignment_id=assignment_id,
            specimen_id=data.specimen_id,
            medtech_id=data.medtech_id,
            assigned_by=assigned_by,
            assigned_at=assignment_row.get("assigned_at", datetime.now(timezone.utc)),
            status="ACTIVE",
        )
