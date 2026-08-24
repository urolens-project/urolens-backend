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
from src.urolens.core.auth_service import hashPassword
from src.urolens.core.encryption import decryptPii, encryptPii
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

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        self.db = db
        self.auditLogger = auditLogger

    async def createPatient(
        self, data: PatientCreateRequest, createdBy: str, request: Request
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
        await self._rejectIfDuplicate(data)

        patientUid = await self._generatePatientUid()

        dob = data.dateOfBirth
        portalPassword = f"{data.lastName.upper()}{dob.day:02d}{dob.month:02d}{dob.year:04d}"
        hashedPw = await asyncio.to_thread(hashPassword, portalPassword)

        portalUser = User(
            username=patientUid,
            hashedPassword=hashedPw,
            role="PATIENT",
            isActive=True,
        )
        self.db.add(portalUser)
        await self.db.flush([portalUser])

        creatorId = uuid.UUID(str(createdBy))
        patient = Patient(
            patientUid=patientUid,
            firstName=encryptPii(data.firstName),
            middleName=encryptPii(data.middleName) if data.middleName else None,
            lastName=encryptPii(data.lastName),
            dateOfBirth=encryptPii(str(data.dateOfBirth)),
            sex=data.sex.value,
            contactNo=encryptPii(data.contactNo) if data.contactNo else None,
            address=encryptPii(data.address) if data.address else None,
            clinicalHistory=data.clinicalHistory,
            isWalkin=data.isWalkin,
            recordFlag="COMPLETE",
            createdBy=creatorId,
            userId=portalUser.userId,
        )
        self.db.add(patient)
        await self.db.flush([patient])

        self.db.add(
            Consent(
                patientId=patient.patientId,
                consentProcess=data.consent.consentGiven,
                consentStorage=data.consent.consentStorage,
                consentResearch=data.consent.consentResearch,
                recordedBy=creatorId,
            )
        )

        await self.auditLogger.record(
            "PATIENT_CREATED",
            entityType="patient",
            entityId=patient.patientId,
            userId=createdBy,
            detailJson={"patient_uid": patientUid},
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(patient)

        return PatientResponse(
            patientId=patient.patientId,
            patientUid=patientUid,
            firstName=data.firstName,
            middleName=data.middleName,
            lastName=data.lastName,
            dateOfBirth=str(data.dateOfBirth),
            sex=data.sex.value,
            contactNo=data.contactNo,
            address=data.address,
            clinicalHistory=data.clinicalHistory,
            isWalkin=data.isWalkin,
            recordFlag="COMPLETE",
            createdAt=patient.createdAt,
            userId=portalUser.userId,
            portalUsername=patientUid,
            portalPassword=portalPassword,
        )

    async def searchPatients(self, q: str) -> list[PatientResponse]:
        """Search patients by decrypted first/last name substring match.

        Args:
            q: Case-insensitive substring to match against first or last name.

        Returns:
            Matching patients, most-recently-created-first is not guaranteed
            (same unordered-scan behavior as the prior implementation).
        """
        rows = (await self.db.execute(select(Patient).limit(_SEARCH_LIMIT))).scalars().all()
        qLower = q.lower()

        responses: list[PatientResponse] = []
        for row in rows:
            try:
                first = decryptPii(row.firstName)
                last = decryptPii(row.lastName)
            except Exception:
                logger.exception("PII decrypt failed for patient row %s", row.patientId)
                continue

            if qLower in first.lower() or qLower in last.lower():
                responses.append(
                    PatientResponse(
                        patientId=row.patientId,
                        patientUid=row.patientUid,
                        firstName=first,
                        middleName=decryptPii(row.middleName) if row.middleName else None,
                        lastName=last,
                        dateOfBirth=decryptPii(row.dateOfBirth),
                        sex=row.sex,
                        contactNo=decryptPii(row.contactNo) if row.contactNo else None,
                        address=decryptPii(row.address) if row.address else None,
                        clinicalHistory=row.clinicalHistory,
                        isWalkin=row.isWalkin,
                        recordFlag=row.recordFlag,
                        createdAt=row.createdAt,
                    )
                )
        return responses

    async def getPatientByUserId(self, userId: str) -> PatientResponse:
        """Look up the patient record linked to a portal account.

        Args:
            user_id: `users.user_id` of the authenticated patient portal
                account (from the auth dependency's resolved claims).

        Returns:
            The patient record linked to this portal account.

        Raises:
            HTTPException: 404 if no patient record is linked to this account.
        """
        stmt = select(Patient).where(Patient.userId == uuid.UUID(str(userId)))
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )

        return PatientResponse(
            patientId=row.patientId,
            patientUid=row.patientUid,
            firstName=decryptPii(row.firstName),
            middleName=decryptPii(row.middleName) if row.middleName else None,
            lastName=decryptPii(row.lastName),
            dateOfBirth=decryptPii(row.dateOfBirth),
            sex=row.sex,
            contactNo=decryptPii(row.contactNo) if row.contactNo else None,
            address=decryptPii(row.address) if row.address else None,
            clinicalHistory=row.clinicalHistory,
            isWalkin=row.isWalkin,
            recordFlag=row.recordFlag,
            createdAt=row.createdAt,
        )

    async def _rejectIfDuplicate(self, data: PatientCreateRequest) -> None:
        """Raise 409 if an existing patient matches on name + date of birth.

        PII is Fernet-encrypted with a random IV, so it can't be matched with
        a `WHERE` clause — this decrypts up to `_DUPLICATE_CHECK_LIMIT` rows
        and compares in Python, same approach (and same limitation beyond
        that row count) as the prior Supabase-REST implementation.

        Raises:
            HTTPException: 409 if a match is found.
        """
        stmt = select(
            Patient.firstName, Patient.lastName, Patient.dateOfBirth
        ).limit(_DUPLICATE_CHECK_LIMIT)
        rows = (await self.db.execute(stmt)).all()
        for firstEnc, lastEnc, dobEnc in rows:
            try:
                if (
                    decryptPii(firstEnc).lower() == data.firstName.lower()
                    and decryptPii(lastEnc).lower() == data.lastName.lower()
                    and decryptPii(dobEnc) == str(data.dateOfBirth)
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

    async def _generatePatientUid(self) -> str:
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
            rows = (await self.db.execute(select(Patient.patientUid))).scalars().all()
            maxNum = 0
            for uid in rows:
                try:
                    num = int(uid.split("-")[-1])
                    if num > maxNum:
                        maxNum = num
                except (ValueError, AttributeError):
                    continue
            candidate = f"PAT-{maxNum + 1:06d}"

            existing = await self.db.execute(
                select(Patient.patientId).where(Patient.patientUid == candidate)
            )
            if existing.scalar_one_or_none() is None:
                return candidate

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate a unique patient UID.",
        )
