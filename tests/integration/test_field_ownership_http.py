"""HTTP-level tests — the phone's field routes only act on the caller's own samples.

Through the real routes, the real services and real signed tokens (the database is a
fake that holds one specimen assigned to another MedTech):
- capture, Proceed, Discard, Submit and reading a field answer 403
  `SPECIMEN_NOT_ASSIGNED` for a second MedTech, with nothing committed and, for a
  capture, no AI analysis run;
- the token's `user_id` is what reaches the ownership check (the owner's own token
  gets past it);
- a receptionist's or physician's real token is refused by the role check before any
  database use.
Role checks with a stubbed user are in tests/test_field_route_stubs.py; the services'
own guards in tests/test_field_capture_decisions.py and tests/test_field_capture_upload.py.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from httpx import AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.models.specimen import Specimen
from src.services.field_capture_service import FieldCaptureService

OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a0a1")
STRANGER_ID = uuid.UUID("00000000-0000-0000-0000-00000000b0b2")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000c0c3")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0d4")
FIELD_ID = uuid.UUID("00000000-0000-0000-0000-00000000e0e5")
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

# (method, path) of every field route the phone calls.
PHONE_WRITE_ROUTES = [
    ("POST", f"/api/v1/fields/{FIELD_ID}/accept"),
    ("POST", f"/api/v1/fields/{FIELD_ID}/discard"),
    ("POST", f"/api/v1/results/{RESULT_ID}/fields/submit"),
]


def _tokenFor(userId: uuid.UUID, role: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


def _auth(userId: uuid.UUID, role: str = "MEDTECH") -> dict:
    return {"Authorization": f"Bearer {_tokenFor(userId, role)}"}


def _ownersSample() -> tuple[Specimen, AnalysisResult, ResultField]:
    specimen = Specimen(specimenId=SPECIMEN_ID, medtechId=OWNER_ID, status="PROCESSING")
    result = AnalysisResult(resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=ResultStatus.CAPTURING)
    field = ResultField(
        fieldId=FIELD_ID, resultId=RESULT_ID, specimenId=SPECIMEN_ID, imageId=uuid.uuid4(),
        fieldNo=1, magnification=Magnification.HPF, status=FieldStatus.ANALYZED,
        aiFindings={}, effectiveCounts={}, confidenceScores={}, imageWidth=800, imageHeight=600,
        version=1, createdAt=NOW, updatedAt=NOW,
    )
    return specimen, result, field


def _sampleDb(specimen: Specimen, result: AnalysisResult, field: ResultField) -> AsyncMock:
    """A session holding one sample: `get` by model, and every `execute` answering
    with the field's specimen id (Proceed, Discard), the field itself (read), or the
    result (capture), whichever the statement selects."""
    db = AsyncMock()
    rows = {Specimen: specimen, AnalysisResult: result, ResultField: field}
    db.get = AsyncMock(side_effect=lambda model, _id, **_kw: rows.get(model))

    def _answer(statement: object, *_args: object, **_kwargs: object) -> MagicMock:
        selected = [c.get("entity") or c.get("type") for c in statement.column_descriptions]
        if selected == [ResultField]:
            value = field
        elif selected == [AnalysisResult]:
            value = result
        else:
            value = field.specimenId
        answer = MagicMock()
        answer.scalar_one_or_none.return_value = value
        answer.scalar_one.return_value = value
        return answer

    db.execute = AsyncMock(side_effect=_answer)
    return db


@pytest.fixture
def fieldsOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)


@pytest.fixture
def sampleDb(fieldsOn: None) -> AsyncGenerator[AsyncMock]:
    """The owner's sample behind `getDb`, removed after the test."""
    db = _sampleDb(*_ownersSample())

    async def _override() -> AsyncGenerator[AsyncMock]:
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


def _assertNotAssigned(response: object) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SPECIMEN_NOT_ASSIGNED"


# ── Another MedTech's sample ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), PHONE_WRITE_ROUTES)
async def test_aSecondMedtechCannotDecideOrSubmitTheOwnersFields(
    asyncClient: AsyncClient, sampleDb: AsyncMock, method: str, path: str
) -> None:
    response = await asyncClient.request(method, path, headers=_auth(STRANGER_ID))

    _assertNotAssigned(response)
    sampleDb.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aSecondMedtechCannotReadTheOwnersField(asyncClient: AsyncClient, sampleDb: AsyncMock) -> None:
    response = await asyncClient.get(f"/api/v1/fields/{FIELD_ID}", headers=_auth(STRANGER_ID))

    _assertNotAssigned(response)
    sampleDb.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aSecondMedtechCannotCaptureIntoTheOwnersSampleAndNoAnalysisRuns(
    asyncClient: AsyncClient, sampleDb: AsyncMock
) -> None:
    with patch.object(FieldCaptureService, "_runInference", AsyncMock()) as inference:
        response = await asyncClient.post(
            "/api/v1/fields",
            headers=_auth(STRANGER_ID),
            data={"specimen_id": str(SPECIMEN_ID), "magnification": "HPF"},
            files={"file": ("field.jpg", JPEG, "image/jpeg")},
        )

    _assertNotAssigned(response)
    inference.assert_not_awaited()
    sampleDb.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_theOwnersTokenGetsPastTheOwnershipCheck(asyncClient: AsyncClient, sampleDb: AsyncMock) -> None:
    # Refused later, by the field-count rules: the token's user_id was the one
    # compared with the specimen's MedTech, and it matched.
    response = await asyncClient.post(
        f"/api/v1/results/{RESULT_ID}/fields/submit", headers=_auth(OWNER_ID)
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] in {"FIELDS_UNDECIDED", "FIELDS_BELOW_MINIMUM"}
    sampleDb.commit.assert_not_awaited()


# ── Other roles, with real tokens ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["RECEPTIONIST", "PHYSICIAN", "PATIENT"])
@pytest.mark.parametrize(("method", "path"), PHONE_WRITE_ROUTES)
async def test_otherRolesAreRefusedBeforeAnyDatabaseUse(
    asyncClient: AsyncClient, fieldsOn: None, role: str, method: str, path: str
) -> None:
    # No getDb override: the shared refuseRealDatabase fixture fails the test on any use.
    response = await asyncClient.request(method, path, headers=_auth(OWNER_ID, role))

    assert response.status_code == 403, response.text
