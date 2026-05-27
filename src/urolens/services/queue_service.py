from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.encryption import decrypt_pii
from src.urolens.core.enums import UserRole
from src.urolens.schemas.queue import (
    MedTechWorkload,
    MedTechWorkloadItem,
    PendingSpecimenItem,
    QueueAssignRequest,
    QueueAssignResponse,
)
from src.urolens.services.notification_service import NotificationService


class QueueService:
    def __init__(
        self,
        db: AsyncClient,
        audit_logger: AuditLogger,
        notification_service: NotificationService,
        sqlalchemy_db: AsyncSession,
    ):
        self.db = db
        self.audit_logger = audit_logger
        self.notification_service = notification_service
        self.sqlalchemy_db = sqlalchemy_db

    async def get_workloads(self) -> list[MedTechWorkload]:
        users_result = await self.db.table("users").select(
            "user_id", "username"
        ).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

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
        ).eq("user_id", str(data.medtech_id)).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

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
            {
                "status": "ASSIGNED",
                "medtech_id": str(data.medtech_id),  # ← add this
                "assigned_at": datetime.now(timezone.utc).isoformat(),  # ← good to track too
            }
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
            db=self.sqlalchemy_db,
            detail_json={
                "specimen_id": str(data.specimen_id),
                "medtech_id": str(data.medtech_id),
            },
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

    # ── Receptionist-facing methods (STORY-WEB-08) ────────────────────────────

    async def get_pending_specimens(self) -> list[PendingSpecimenItem]:
        """Return all LABELED specimens with decrypted patient PII and test type.
        Ordered by received_at ascending (FIFO).
        """
        spec_res = await self.db.table("specimens").select(
            "specimen_id, sample_uid, status, received_at, lab_request_id"
        ).eq("status", "LABELED").order("received_at", desc=False).execute()

        specimens = spec_res.data or []
        if not specimens:
            return []

        lab_request_ids = list({str(s["lab_request_id"]) for s in specimens if s.get("lab_request_id")})
        lr_res = await self.db.table("lab_requests").select(
            "lab_request_id, test_type, patient_id"
        ).in_("lab_request_id", lab_request_ids).execute()

        lr_map = {str(lr["lab_request_id"]): lr for lr in (lr_res.data or [])}

        patient_ids = list({str(lr["patient_id"]) for lr in (lr_res.data or []) if lr.get("patient_id")})
        pat_map: dict[str, dict] = {}
        if patient_ids:
            pat_res = await self.db.table("patients").select(
                "patient_id, first_name, last_name"
            ).in_("patient_id", patient_ids).execute()
            pat_map = {str(p["patient_id"]): p for p in (pat_res.data or [])}

        items: list[PendingSpecimenItem] = []
        for spec in specimens:
            lr = lr_map.get(str(spec.get("lab_request_id", "")), {})
            pat = pat_map.get(str(lr.get("patient_id", "")), {})

            try:
                first = decrypt_pii(pat["first_name"]) if pat.get("first_name") else ""
                last = decrypt_pii(pat["last_name"]) if pat.get("last_name") else ""
            except Exception:
                first = last = ""
            patient_name = f"{first} {last}".strip() or "Unknown Patient"

            items.append(PendingSpecimenItem(
                specimen_id=UUID(spec["specimen_id"]),
                sample_uid=spec.get("sample_uid") or spec["specimen_id"][:8].upper(),
                patient_name=patient_name,
                test_type=lr.get("test_type") or "—",
                received_at=spec["received_at"],
                status=spec["status"],
            ))

        return items

    async def get_receptionist_workloads(self) -> list[MedTechWorkloadItem]:
        """Return all active MedTechs with their active specimen queue depth.
        Active = specimens.status IN (ASSIGNED, IN_QUEUE, PROCESSING).
        Sorted ascending by active_count (least-loaded first).
        """
        users_res = await self.db.table("users").select(
            "user_id, username"
        ).eq("role", UserRole.MEDTECH).eq("is_active", True).execute()

        medtechs = users_res.data or []
        if not medtechs:
            return []

        medtech_ids = [str(m["user_id"]) for m in medtechs]

        qa_res = await self.db.table("queue_assignments").select(
            "medtech_id, specimen_id"
        ).in_("medtech_id", medtech_ids).execute()

        qa_rows = qa_res.data or []
        active_spec_ids: set[str] = set()

        if qa_rows:
            spec_ids = list({str(qa["specimen_id"]) for qa in qa_rows})
            spec_res = await self.db.table("specimens").select(
                "specimen_id"
            ).in_("specimen_id", spec_ids).in_(
                "status", ["ASSIGNED", "IN_QUEUE", "PROCESSING"]
            ).execute()
            active_spec_ids = {str(s["specimen_id"]) for s in (spec_res.data or [])}

        medtech_counts: dict[str, int] = {m["user_id"]: 0 for m in medtechs}
        for qa in qa_rows:
            if str(qa["specimen_id"]) in active_spec_ids:
                mid = str(qa["medtech_id"])
                medtech_counts[mid] = medtech_counts.get(mid, 0) + 1

        items = [
            MedTechWorkloadItem(
                user_id=UUID(m["user_id"]),
                full_name=m["username"],
                active_count=medtech_counts.get(m["user_id"], 0),
            )
            for m in medtechs
        ]
        items.sort(key=lambda x: x.active_count)
        return items
