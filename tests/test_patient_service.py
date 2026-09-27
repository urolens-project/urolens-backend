"""Unit tests — patient_service.PatientService.create_patient and the
PatientCreateRequest/ConsentData schema validators added alongside it.

Schema-level rules (blank name, future DOB, unconfirmed consent) are pure
Pydantic validation, so those are exercised directly against the schema
rather than through the service. Service-level tests cover the duplicate
check and the success path, following the AsyncMock/MagicMock(spec=Model)
conventions in tests/test_lab_request_service.py.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import ConflictException
from src.schemas.patient import ConsentData, PatientCreateRequest, SexEnum
from src.services.patient_service import PatientService

CREATOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000050")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000051")
PORTAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000052")


def _validConsent() -> dict:
    return {"consentGiven": True, "consentStorage": True, "consentResearch": True}


def _validPayload(**overrides) -> dict:
    payload = {
        "firstName": "Jane",
        "lastName": "Doe",
        "dateOfBirth": date(2000, 1, 1),
        "sex": SexEnum.FEMALE,
        "isWalkin": False,
        "consent": ConsentData(**_validConsent()),
    }
    payload.update(overrides)
    return payload


# ── Schema validation ───────────────────────────────────────────────────────


def test_dateOfBirthInFutureRejected():
    with pytest.raises(ValidationError, match="dateOfBirth"):
        PatientCreateRequest(**_validPayload(dateOfBirth=date.today() + timedelta(days=1)))


def test_dateOfBirthTodayRejected():
    """'must be a date in the past' excludes today, not just future dates."""
    with pytest.raises(ValidationError, match="dateOfBirth"):
        PatientCreateRequest(**_validPayload(dateOfBirth=date.today()))


@pytest.mark.parametrize("field", ["firstName", "lastName"])
def test_blankNameRejected(field):
    with pytest.raises(ValidationError, match=field):
        PatientCreateRequest(**_validPayload(**{field: "   "}))


def test_middleNameBlankIsUnaffected():
    """Only firstName/lastName are required-non-blank; middleName stays optional."""
    req = PatientCreateRequest(**_validPayload(middleName=None))
    assert req.middleName is None


@pytest.mark.parametrize("field", ["consentGiven", "consentStorage", "consentResearch"])
def test_unconfirmedConsentRejected(field):
    consent = _validConsent()
    consent[field] = False
    with pytest.raises(ValidationError, match=field):
        ConsentData(**consent)


def test_allConsentsFalseReportsEachFieldIndependently():
    """All three unconfirmed at once must each surface their own error, not
    just the first one Pydantic happens to hit.
    """
    with pytest.raises(ValidationError) as excInfo:
        ConsentData(consentGiven=False, consentStorage=False, consentResearch=False)
    fieldsReported = {str(e["loc"][-1]) for e in excInfo.value.errors()}
    assert fieldsReported == {"consentGiven", "consentStorage", "consentResearch"}


def test_validPayloadPasses():
    req = PatientCreateRequest(**_validPayload())
    assert req.firstName == "Jane"
    assert req.consent.consentGiven is True


# ── Service: duplicate detection ────────────────────────────────────────────


def _makeDb(duplicateFound: bool = False) -> AsyncMock:
    """Configures db.execute to answer, in call order:
    1. the duplicate-check dedup_hash lookup (`.scalar_one_or_none()`)
    2. the UID-generation existing-UIDs scan (`.scalars().all()`)
    3. the UID-generation collision check (`.scalar_one_or_none()`)

    All three are set on every mocked result regardless of which one a given
    call actually needs, so a single list of results can serve calls that
    only occur when no duplicate is found.
    """

    def _result(allRows=None, scalarOneOrNone=None):
        result = MagicMock()
        result.all.return_value = allRows or []
        result.scalars.return_value.all.return_value = allRows or []
        result.scalar_one_or_none.return_value = scalarOneOrNone
        return result

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _result(scalarOneOrNone=PATIENT_ID if duplicateFound else None),  # duplicate check
            _result(allRows=[]),  # existing patient_uid scan -> none, so PAT-000001
            _result(scalarOneOrNone=None),  # candidate UID not taken
        ]
    )
    db.add = MagicMock()
    db.rollback = AsyncMock()

    async def _flush(objs):
        for obj in objs:
            if hasattr(obj, "userId") and obj.__class__.__name__ == "User":
                obj.userId = PORTAL_USER_ID
            else:
                obj.patientId = PATIENT_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    def _refresh(obj):
        obj.createdAt = "2026-09-23T00:00:00+00:00"

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.mark.asyncio
async def test_createPatientDuplicateRaisesConflict():
    dob = date(2000, 1, 1)
    db = _makeDb(duplicateFound=True)
    service = PatientService(db=db, auditLogger=AsyncMock())
    request = PatientCreateRequest(**_validPayload(dateOfBirth=dob))

    with pytest.raises(ConflictException) as excInfo:
        await service.createPatient(request, str(CREATOR_ID), request=MagicMock())

    assert excInfo.value.errorCode == "DUPLICATE_PATIENT"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejectIfDuplicateIsNotRowCountLimited():
    """Regression guard for the bug this replaces: the old check only
    decrypted/compared the first 100 rows (`.limit(100)`), so a duplicate
    past that point was silently missed. The dedup_hash lookup is an
    indexed equality match, not a row scan, so it has no row-count cap at
    all — assert on the compiled statement (not row data) so this fails
    loudly if a `.limit(...)` is ever reintroduced, regardless of how many
    patients exist.
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    service = PatientService(db=db, auditLogger=AsyncMock())

    dedupHash = PatientService._computeDedupHash("Jane", "Doe", date(2000, 1, 1))
    await service._rejectIfDuplicate(dedupHash)

    db.execute.assert_awaited_once()
    compiledStmt = str(db.execute.await_args.args[0])
    assert "dedup_hash" in compiledStmt
    assert "LIMIT" not in compiledStmt.upper()


def test_computeDedupHashIsCaseAndWhitespaceInsensitive():
    dob = date(2000, 1, 1)
    a = PatientService._computeDedupHash(" Jane ", "Doe", dob)
    b = PatientService._computeDedupHash("jane", "DOE", dob)
    assert a == b


def test_computeDedupHashDiffersOnDifferentInput():
    dob = date(2000, 1, 1)
    a = PatientService._computeDedupHash("Jane", "Doe", dob)
    b = PatientService._computeDedupHash("Jane", "Doe", date(2000, 1, 2))
    assert a != b


@pytest.mark.asyncio
async def test_createPatientRaceConditionOnFlushRaisesDuplicateConflict():
    """The pre-check passes (no existing row), but the unique-constrained
    flush still fails — simulating two concurrent creates for the same
    person. This must surface as the same 409 DUPLICATE_PATIENT, not a raw
    IntegrityError.
    """
    db = _makeDb(duplicateFound=False)

    async def _flush(objs):
        for obj in objs:
            if obj.__class__.__name__ == "User":
                obj.userId = PORTAL_USER_ID
            else:
                raise IntegrityError("insert", {}, Exception("unique violation"))

    db.flush = AsyncMock(side_effect=_flush)
    service = PatientService(db=db, auditLogger=AsyncMock())
    request = PatientCreateRequest(**_validPayload())

    with pytest.raises(ConflictException) as excInfo:
        await service.createPatient(request, str(CREATOR_ID), request=MagicMock())

    assert excInfo.value.errorCode == "DUPLICATE_PATIENT"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_createPatientSuccessGeneratesUidAndPersistsConsent():
    db = _makeDb()
    auditLogger = AsyncMock()
    service = PatientService(db=db, auditLogger=auditLogger)
    request = PatientCreateRequest(**_validPayload())

    response = await service.createPatient(request, str(CREATOR_ID), request=MagicMock())

    assert response.patientUid == "PAT-000001"
    assert response.firstName == "Jane"
    assert response.portalUsername == "PAT-000001"
    assert response.portalPassword is not None
    assert len(response.portalPassword) == 10
    # Not derived from name/DOB (the guessable pattern this replaces).
    assert "DOE" not in response.portalPassword
    assert "20000101" not in response.portalPassword
    db.commit.assert_awaited_once()
    auditLogger.record.assert_awaited_once()
    assert db.add.call_count == 3  # portal user, patient, consent


def test_generateOtpPasswordIsRandomAndUnambiguous():
    passwords = {PatientService._generateOtpPassword() for _ in range(50)}
    assert len(passwords) == 50  # no collisions across 50 draws
    for pw in passwords:
        assert len(pw) == 10
        assert not (set(pw) & set("IO01"))
