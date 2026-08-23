"""Patient intake: creation (with linked portal account and consent record),
search, and portal-account-linked lookup.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.auth_service import hash_password
from src.urolens.core.encryption import decrypt_pii, encrypt_pii
from src.urolens.models.consent import Consent
from src.urolens.models.patient import Patient
from src.urolens.models.user import User
from src.urolens.schemas.patient import PatientCreateRequest, PatientResponse

_UID_GENERATION_ATTEMPTS = 5
_DUPLICATE_CHECK_LIMIT = 100
_SEARCH_LIMIT = 20

logger = logging.getLogger(__name__)

class PatientService:
    """Owns patient creation, search, and portal-account-linked lookup.

    Create writes a portal user, the patient row, and a consent record in one
    SQLAlchemy transaction, committed once. This replaces the previous
    Supabase-REST implementation's manual compensating deletes (Supabase REST
    has no cross-table transaction) with a real rollback-on-failure: nothing
    is committed unless every step succeeds.
    """

    def __init__(self, db: AsyncSession, audit_logger: AuditLogger) -> None:
        self.db = db
        self.audit_logger = audit_logger

    async def create_patient(
        self, data: PatientCreateRequest, created_by: str, request: Request
    ) -> PatientResponse:
        """Create a patient record, portal account, and consent record.

        Args:
            data: Validated patient intake data, including consent answers.
            created_by: user_id of the authenticated receptionist creating
                the record (from the auth dependency's resolved claims).
            request: Inbound request, forwarded to the audit logger for IP
                attribution.

        Returns:
            The created patient, including the generated portal username and
            one-time plaintext portal password (never re-derivable after
            this call — the stored hash cannot be reversed).

        Raises:
            HTTPException: 409 if a patient with the same name and date of
                birth already exists; 500 if the portal account could not be
                created or a unique patient UID could not be generated.
        """
        await self._reject_if_duplicate(data)

        patient_uid = await self._generate_patient_uid()

        dob = data.date_of_birth
        portal_password = f"{data.last_name.upper()}{dob.day:02d}{dob.month:02d}{dob.year:04d}"
        hashed_pw = await asyncio.to_thread(hash_password, portal_password)

        portal_user = User(
            username=patient_uid,
            hashed_password=hashed_pw,
            role="PATIENT",
            is_active=True,
        )
        self.db.add(portal_user)
        await self.db.flush([portal_user])

        creator_id = uuid.UUID(str(created_by))
        patient = Patient(
            patient_uid=patient_uid,
            first_name=encrypt_pii(data.first_name),
            middle_name=encrypt_pii(data.middle_name) if data.middle_name else None,
            last_name=encrypt_pii(data.last_name),
            date_of_birth=encrypt_pii(str(data.date_of_birth)),
            sex=data.sex.value,
            contact_no=encrypt_pii(data.contact_no) if data.contact_no else None,
            address=encrypt_pii(data.address) if data.address else None,
            clinical_history=data.clinical_history,
            is_walkin=data.is_walkin,
            record_flag="COMPLETE",
            created_by=creator_id,
            user_id=portal_user.user_id,
        )
        self.db.add(patient)
        await self.db.flush([patient])

        self.db.add(
            Consent(
                patient_id=patient.patient_id,
                consent_process=data.consent.consent_given,
                consent_storage=data.consent.consent_storage,
                consent_research=data.consent.consent_research,
                recorded_by=creator_id,
            )
        )

        await self.audit_logger.record(
            "PATIENT_CREATED",
            entity_type="patient",
            entity_id=patient.patient_id,
            user_id=created_by,
            detail_json={"patient_uid": patient_uid},
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(patient)

        return PatientResponse(
            patient_id=patient.patient_id,
            patient_uid=patient_uid,
            first_name=data.first_name,
            middle_name=data.middle_name,
            last_name=data.last_name,
            date_of_birth=str(data.date_of_birth),
            sex=data.sex.value,
            contact_no=data.contact_no,
            address=data.address,
            clinical_history=data.clinical_history,
            is_walkin=data.is_walkin,
            record_flag="COMPLETE",
            created_at=patient.created_at,
            user_id=portal_user.user_id,
            portal_username=patient_uid,
            portal_password=portal_password,
        )

    async def search_patients(self, q: str) -> list[PatientResponse]:
        """Search patients by decrypted first/last name substring match.

        Args:
            q: Case-insensitive substring to match against first or last name.

        Returns:
            Matching patients, most-recently-created-first is not guaranteed
            (same unordered-scan behavior as the prior implementation).
        """
        rows = (await self.db.execute(select(Patient).limit(_SEARCH_LIMIT))).scalars().all()
        q_lower = q.lower()

        responses: list[PatientResponse] = []
        for row in rows:
            try:
                first = decrypt_pii(row.first_name)
                last = decrypt_pii(row.last_name)
            except Exception:
                logger.exception("PII decrypt failed for patient row %s", row.patient_id)
                continue

            if q_lower in first.lower() or q_lower in last.lower():
                responses.append(
                    PatientResponse(
                        patient_id=row.patient_id,
                        patient_uid=row.patient_uid,
                        first_name=first,
                        middle_name=decrypt_pii(row.middle_name) if row.middle_name else None,
                        last_name=last,
                        date_of_birth=decrypt_pii(row.date_of_birth),
                        sex=row.sex,
                        contact_no=decrypt_pii(row.contact_no) if row.contact_no else None,
                        address=decrypt_pii(row.address) if row.address else None,
                        clinical_history=row.clinical_history,
                        is_walkin=row.is_walkin,
                        record_flag=row.record_flag,
                        created_at=row.created_at,
                    )
                )
        return responses

    async def get_patient_by_user_id(self, user_id: str) -> PatientResponse:
        """Look up the patient record linked to a portal account.

        Args:
            user_id: `users.user_id` of the authenticated patient portal
                account (from the auth dependency's resolved claims).

        Returns:
            The patient record linked to this portal account.

        Raises:
            HTTPException: 404 if no patient record is linked to this account.
        """
        stmt = select(Patient).where(Patient.user_id == uuid.UUID(str(user_id)))
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )

        return PatientResponse(
            patient_id=row.patient_id,
            patient_uid=row.patient_uid,
            first_name=decrypt_pii(row.first_name),
            middle_name=decrypt_pii(row.middle_name) if row.middle_name else None,
            last_name=decrypt_pii(row.last_name),
            date_of_birth=decrypt_pii(row.date_of_birth),
            sex=row.sex,
            contact_no=decrypt_pii(row.contact_no) if row.contact_no else None,
            address=decrypt_pii(row.address) if row.address else None,
            clinical_history=row.clinical_history,
            is_walkin=row.is_walkin,
            record_flag=row.record_flag,
            created_at=row.created_at,
        )

    async def _reject_if_duplicate(self, data: PatientCreateRequest) -> None:
        """Raise 409 if an existing patient matches on name + date of birth.

        PII is Fernet-encrypted with a random IV, so it can't be matched with
        a `WHERE` clause — this decrypts up to `_DUPLICATE_CHECK_LIMIT` rows
        and compares in Python, same approach (and same limitation beyond
        that row count) as the prior Supabase-REST implementation.

        Raises:
            HTTPException: 409 if a match is found.
        """
        stmt = select(
            Patient.first_name, Patient.last_name, Patient.date_of_birth
        ).limit(_DUPLICATE_CHECK_LIMIT)
        rows = (await self.db.execute(stmt)).all()
        for first_enc, last_enc, dob_enc in rows:
            try:
                if (
                    decrypt_pii(first_enc).lower() == data.first_name.lower()
                    and decrypt_pii(last_enc).lower() == data.last_name.lower()
                    and decrypt_pii(dob_enc) == str(data.date_of_birth)
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A patient with this name and date of birth already exists.",
                    )
            except HTTPException:
                raise
            except Exception:
                logger.exception("PII decrypt failed for patient row")
                continue

    async def _generate_patient_uid(self) -> str:
        """Generate a sequential `PAT-NNNNNN` patient UID, safe under
        concurrent inserts.

        Same check-then-retry-on-collision idiom as
        `specimen_service._generate_sample_uid` and
        `lab_request_service._generate_request_uid`: compute a candidate,
        verify it isn't already taken with a direct existence check, retry
        on collision. Unlike those two, the candidate here is the next
        sequential number (not a random suffix) — this only changes how the
        candidate is computed, not the check-and-retry safety idiom being
        reused. Re-scans on every attempt so a collision is resolved against
        the now-current max, not a stale one.

        Returns:
            A `patient_uid` not currently present in the `patients` table.

        Raises:
            HTTPException: 500 if no unique UID could be generated after
                `_UID_GENERATION_ATTEMPTS` attempts.
        """
        for _ in range(_UID_GENERATION_ATTEMPTS):
            rows = (await self.db.execute(select(Patient.patient_uid))).scalars().all()
            max_num = 0
            for uid in rows:
                try:
                    num = int(uid.split("-")[-1])
                    if num > max_num:
                        max_num = num
                except (ValueError, AttributeError):
                    continue
            candidate = f"PAT-{max_num + 1:06d}"

            existing = await self.db.execute(
                select(Patient.patient_id).where(Patient.patient_uid == candidate)
            )
            if existing.scalar_one_or_none() is None:
                return candidate

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate a unique patient UID.",
        )
