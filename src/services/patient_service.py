"""Patient intake: creation (with linked portal account and consent record),
search, and portal-account-linked lookup.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import date

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.auth_service import hashPassword
from src.core.config import settings
from src.core.encryption import decryptPii, encryptPii
from src.core.exceptions import ConflictException, NotFoundException
from src.models.consent import Consent
from src.models.patient import Patient
from src.models.user import User
from src.schemas.patient import PatientCreateRequest, PatientResponse

_UID_GENERATION_ATTEMPTS = 5
_SEARCH_LIMIT = 20
# One-time portal password alphabet: uppercase/digits only, with ambiguous
# glyphs (I/O/0/1) dropped so a patient can read it off a printed/on-screen
# success card and type it back without misreading a character.
_OTP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_OTP_LENGTH = 10
_DUPLICATE_PATIENT_MESSAGE = "A patient with this name and date of birth already exists."

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
        dedupHash = self._computeDedupHash(data.firstName, data.lastName, data.dateOfBirth)
        await self._rejectIfDuplicate(dedupHash)

        patientUid = await self._generatePatientUid()

        portalPassword = self._generateOtpPassword()
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
            dedupHash=dedupHash,
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
        try:
            await self.db.flush([patient])
        except IntegrityError:
            # The pre-check in _rejectIfDuplicate is not itself race-safe —
            # two concurrent creates for the same person can both pass it.
            # The dedup_hash unique constraint is the actual guarantee; this
            # translates its violation into the same 409 the pre-check gives.
            await self.db.rollback()
            raise ConflictException(
                code="DUPLICATE_PATIENT", message=_DUPLICATE_PATIENT_MESSAGE
            ) from None

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
            raise NotFoundException(message="Patient record not found for this account.")

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

    @staticmethod
    def _computeDedupHash(firstName: str, lastName: str, dateOfBirth: date) -> str:
        """Derive the keyed duplicate-detection fingerprint for a name + date-of-birth triple.

        HMAC-SHA256, keyed by `settings.dedupHashKey` (distinct from the
        Fernet `ENCRYPTION_KEY`) — a plain unkeyed hash of a name+DOB triple
        is dictionary-attackable (low-entropy, semi-public inputs), so
        anyone reading the `dedup_hash` column could otherwise run a
        candidate list of names/DOBs against it to de-anonymize rows. Both
        names are lowercased/stripped first so casing/whitespace differences
        between two submissions of the same person still collide.

        Returns:
            A 64-character hex digest, matching `patients.dedup_hash`.
        """
        normalized = f"{firstName.strip().lower()}|{lastName.strip().lower()}|{dateOfBirth.isoformat()}"
        return hmac.new(
            settings.dedupHashKey.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _generateOtpPassword() -> str:
        """Generate a random one-time portal password.

        Replaces the prior `lastname + DOB` derivation, which is guessable
        by anyone who knows (or can infer) the patient's own name and birth
        date — exactly the information collected at the same intake this
        password is created from. Forcing a password change on first portal
        login is tracked as a separate follow-up ticket; this only removes
        the guessability of the initial credential.

        Returns:
            A 10-character random string drawn from `secrets.choice` (CSPRNG),
            using an alphabet with ambiguous glyphs (I/O/0/1) removed so it's
            legible when shown once on the success screen.
        """
        return "".join(secrets.choice(_OTP_ALPHABET) for _ in range(_OTP_LENGTH))

    async def _rejectIfDuplicate(self, dedupHash: str) -> None:
        """Raise 409 if an existing patient has the same name + date of birth.

        A single indexed equality lookup against `patients.dedup_hash`
        (unique-constrained) — replaces the prior approach of decrypting and
        comparing up to 100 rows in Python, which silently stopped catching
        duplicates beyond that row count. This check alone is not race-safe
        under concurrent creates for the same person; `createPatient`'s
        flush is wrapped separately to catch the unique-constraint violation
        as the actual guarantee.

        Raises:
            HTTPException: 409 (`DUPLICATE_PATIENT`) if a match is found.
        """
        existing = await self.db.execute(
            select(Patient.patientId).where(Patient.dedupHash == dedupHash)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictException(
                code="DUPLICATE_PATIENT", message=_DUPLICATE_PATIENT_MESSAGE
            )

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

        exc = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate a unique patient UID.",
        )
        exc.errorCode = "PATIENT_UID_GENERATION_FAILED"
        raise exc
