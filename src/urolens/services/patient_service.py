from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.encryption import encrypt_pii, decrypt_pii
from src.urolens.schemas.patient import PatientCreateRequest, PatientResponse


class PatientService:
    def __init__(self, db: AsyncClient, audit_logger: AuditLogger):
        self.db = db
        self.audit_logger = audit_logger

    async def create_patient(
        self, data: PatientCreateRequest, created_by: str, request: Request
    ) -> PatientResponse:
        existing = await self.db.table("patients").select("first_name", "last_name", "date_of_birth").limit(100).execute()
        for row in (existing.data or []):
            try:
                if (
                    decrypt_pii(row["first_name"]).lower() == data.first_name.lower()
                    and decrypt_pii(row["last_name"]).lower() == data.last_name.lower()
                    and decrypt_pii(row["date_of_birth"]) == str(data.date_of_birth)
                ):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "DUPLICATE_PATIENT",
                                "message": "A patient with this name and date of birth already exists.",
                                "details": {},
                            }
                        },
                    )
            except HTTPException:
                raise
            except Exception:
                continue

        encrypted_first = encrypt_pii(data.first_name)
        encrypted_last = encrypt_pii(data.last_name)
        encrypted_dob = encrypt_pii(str(data.date_of_birth))

        patient_uid = await self._generate_patient_uid()

        patient_payload = {
            "patient_uid": patient_uid,
            "first_name": encrypted_first,
            "last_name": encrypted_last,
            "date_of_birth": encrypted_dob,
            "sex": "OTHER",
            "contact_no": encrypt_pii(data.contact_no) if data.contact_no else None,
            "address": encrypt_pii(data.address) if data.address else None,
            "is_walkin": False,
            "record_flag": "COMPLETE",
            "registered_by": str(created_by),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        patient_result = await self.db.table("patients").insert(patient_payload).execute()
        if not patient_result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create patient record.",
            )

        patient_row = patient_result.data[0]
        patient_id = patient_row["patient_id"]

        try:
            consent_payload = {
                "patient_id": patient_id,
                "consent_process": data.consent.consent_given,
                "consent_storage": data.consent.consent_storage,
                "consent_research": data.consent.consent_research,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "recorded_by": str(created_by),
            }
            consent_result = await self.db.table("consents").insert(consent_payload).execute()
            if not consent_result.data:
                await self.db.table("patients").delete().eq("patient_id", patient_id).execute()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create consent record.",
                )
        except Exception:
            await self.db.table("patients").delete().eq("patient_id", patient_id).execute()
            raise

        await self.audit_logger.record(
            "PATIENT_CREATED",
            entity_type="patient",
            entity_id=patient_id,
            user_id=created_by,
            detail_json={"patient_uid": patient_uid},
            db=self.db,
            request=request,
        )

        return PatientResponse(
            patient_id=patient_id,
            patient_uid=patient_uid,
            first_name=data.first_name,
            last_name=data.last_name,
            date_of_birth=str(data.date_of_birth),
            contact_no=data.contact_no,
            address=data.address,
            is_walkin=False,
            record_flag="COMPLETE",
            created_at=patient_row.get("created_at", datetime.now(timezone.utc)),
        )

    async def search_patients(self, q: str) -> list[PatientResponse]:
        result = await self.db.table("patients").select("*").limit(20).execute()
        rows = result.data or []
        q_lower = q.lower()

        responses: list[PatientResponse] = []
        for row in rows:
            try:
                first = decrypt_pii(row["first_name"])
                last = decrypt_pii(row["last_name"])
            except Exception:
                continue

            if q_lower in first.lower() or q_lower in last.lower():
                responses.append(PatientResponse(
                    patient_id=row["patient_id"],
                    patient_uid=row["patient_uid"],
                    first_name=first,
                    last_name=last,
                    date_of_birth=decrypt_pii(row["date_of_birth"]),
                    contact_no=decrypt_pii(row["contact_no"]) if row.get("contact_no") else None,
                    address=decrypt_pii(row["address"]) if row.get("address") else None,
                    is_walkin=row.get("is_walkin", False),
                    record_flag=row.get("record_flag"),
                    created_at=row.get("created_at", datetime.now(timezone.utc)),
                ))

        return responses

    async def _generate_patient_uid(self) -> str:
        result = await self.db.table("patients").select("patient_uid").execute()
        max_num = 0
        for row in (result.data or []):
            uid = row.get("patient_uid", "")
            try:
                num = int(uid.split("-")[-1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
        return f"PAT-{max_num + 1:06d}"
