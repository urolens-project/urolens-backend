"""Unit tests — the MedTech's assigned queue data (UROLENS-225).

The mobile queue is built from `GET /sync/pull`, and `GET /results/medtech/pending`
is the online equivalent. Covered:
- `decryptStoredPii`: ciphertext decrypted, legacy plaintext passed through,
  undecryptable ciphertext never returned.
- sync payload: no patient names in any form (the app shows the patient code)
  and the latest supervisor `return_reason` on returned results.
- the pending list: sample ID and queue fields per row, no patient name,
  latest return reason, status filter, returned-first + received-time order,
  and a real COUNT for the total.
DB and Supabase are mocked.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

from src.core.encryption import decryptStoredPii, encryptPii
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.patient import Patient
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen
from src.services import sync_service
from src.services.result_confirmation_service import ResultConfirmationService
from tests.conftest import makeSyncDb

MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ── decryptStoredPii ──────────────────────────────────────────────────────────

def test_encryptedValueIsDecrypted():
    assert decryptStoredPii(encryptPii("Juan Dela Cruz")) == "Juan Dela Cruz"


def test_legacyPlaintextPassesThroughUnchanged():
    assert decryptStoredPii("Maria Santos") == "Maria Santos"


def test_ciphertextThatCannotBeDecryptedIsNeverReturned():
    foreign = Fernet(Fernet.generate_key()).encrypt(b"Juan Dela Cruz").decode()  # other key
    assert decryptStoredPii(foreign) is None


@pytest.mark.parametrize("empty", [None, ""])
def test_emptyValuesGiveNone(empty):
    assert decryptStoredPii(empty) is None


def test_ourCiphertextAlwaysHasTheDetectedPrefix():
    # The legacy-plaintext check relies on every Fernet token starting "gAAAAA".
    assert all(encryptPii(f"name {i}").startswith("gAAAAA") for i in range(20))


# ── Sync payload ──────────────────────────────────────────────────────────────

def _makeSyncSupabase(specimenRows: list[dict], resultRows: list[dict]) -> MagicMock:
    def _table(name: str) -> MagicMock:
        query = MagicMock()
        for method in ("select", "eq", "gt", "in_"):
            getattr(query, method).return_value = query
        data = {"specimens": specimenRows, "analysis_results": resultRows}.get(name, [])
        query.execute = AsyncMock(return_value=MagicMock(data=data))
        return query

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


async def _pull(db, specimens, results):
    with patch.object(sync_service, "supabase", _makeSyncSupabase(specimens, results)), \
         patch.object(sync_service, "AuditLogger", return_value=MagicMock(record=AsyncMock())):
        return await sync_service.pull(db, str(MEDTECH_ID), None)


@pytest.mark.asyncio
async def test_syncNeverSendsPatientNamesInAnyForm():
    # The app shows only the patient code, so the name has no reason to be on
    # the phone — not as plaintext, not as ciphertext (RA 10173 minimization).
    # Even if a row carried a name, it must not come through.
    encrypted, legacy, none = (str(uuid.uuid4()) for _ in range(3))
    specimens = [
        {"specimen_id": encrypted, "patient_uid": "PAT-000001", "patient_name": encryptPii("Juan Dela Cruz")},
        {"specimen_id": legacy, "patient_uid": "PAT-000002", "patient_name": "Maria Santos"},
        {"specimen_id": none, "patient_uid": "PAT-000003"},
    ]

    payload = await _pull(makeSyncDb(), specimens, [])

    rows = payload["changes"]["specimens"]["created"]
    # Kept as "" (not dropped/null): the app's local column is a required string.
    assert {r["id"]: r["patient_name"] for r in rows} == {encrypted: "", legacy: "", none: ""}
    assert {r["patient_uid"] for r in rows} == {"PAT-000001", "PAT-000002", "PAT-000003"}
    body = json.dumps(payload)
    assert "Juan" not in body and "Maria" not in body and "gAAAAA" not in body


def test_syncDoesNotEvenReadThePatientNameColumn():
    # Minimization at the source: the Supabase select never asks for it.
    columns = {c.strip() for c in sync_service._SPECIMEN_COLS.split(",")}
    assert "patient_name" not in columns
    assert {"patient_uid", "sample_uid", "status"} <= columns


@pytest.mark.asyncio
async def test_returnedResultsCarryTheLatestSupervisorReason():
    returnedId = uuid.uuid4()
    pendingId = uuid.uuid4()
    specimenId = str(uuid.uuid4())
    specimen = {"specimen_id": specimenId, "patient_name": None}
    results = [
        {"result_id": str(returnedId), "specimen_id": specimenId, "status": "RETURNED_FOR_CORRECTION"},
        {"result_id": str(pendingId), "specimen_id": specimenId, "status": "PENDING_CONFIRM"},
    ]
    # Newest first, as the query orders them: the first reason per result wins.
    db = makeSyncDb(reasons=[(returnedId, "Recount RBC"), (returnedId, "Older reason")])

    payload = await _pull(db, [specimen], results)

    reasons = {r["id"]: r["return_reason"] for r in payload["changes"]["analysisResults"]["created"]}
    assert reasons == {str(returnedId): "Recount RBC", str(pendingId): None}
    query = db.execute.await_args_list[0].args[0]
    assert "ORDER BY result_returns.returned_at DESC" in _sql(query)


@pytest.mark.asyncio
async def test_noReturnReasonQueryWhenNothingIsReturned():
    db = makeSyncDb()
    specimenId = str(uuid.uuid4())
    results = [{"result_id": str(uuid.uuid4()), "specimen_id": specimenId, "status": "PENDING_CONFIRM"}]

    payload = await _pull(db, [{"specimen_id": specimenId, "patient_name": None}], results)

    assert payload["changes"]["analysisResults"]["created"][0]["return_reason"] is None
    queried = [c.args[0].column_descriptions[0]["entity"] for c in db.execute.await_args_list]
    assert ResultReturn not in queried


# ── GET /results/medtech/pending ──────────────────────────────────────────────

def _makePendingRow(status: str, sampleUid: str, patientName: str | None, receivedAt: datetime):
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = uuid.uuid4()
    ar.specimenId = uuid.uuid4()
    ar.status = status
    spec = MagicMock(spec=Specimen)
    spec.specimenId = ar.specimenId
    spec.sampleUid = sampleUid
    spec.testType = "Urinalysis"
    spec.priorityLevel = "ROUTINE"
    spec.receivedAt = receivedAt
    spec.patientUid = "PAT-000001"
    spec.patientName = patientName
    return ar, spec


def _makePendingDb(total: int, rows: list, returns: list | None = None) -> AsyncMock:
    countResult = MagicMock(scalar_one=MagicMock(return_value=total))
    rowsResult = MagicMock(all=MagicMock(return_value=rows))
    returnsResult = MagicMock()
    returnsResult.scalars.return_value.all.return_value = returns or []
    patient = MagicMock(spec=Patient)
    patient.patientUid = "PAT-000001"
    patient.dateOfBirth = encryptPii("1990-05-01")
    patient.sex = "FEMALE"
    patientsResult = MagicMock()
    patientsResult.scalars.return_value.all.return_value = [patient]
    effects = [countResult, rowsResult] + ([returnsResult] if returns is not None else []) + [patientsResult]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=effects)
    return db


def _service(db) -> ResultConfirmationService:
    return ResultConfirmationService(
        db=db, auditLogger=MagicMock(record=AsyncMock()), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )


@pytest.mark.asyncio
async def test_pendingRowsHaveTheSampleIdAndQueueFieldsButNoPatientName():
    received = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
    rows = [
        _makePendingRow("PENDING_CONFIRM", "SMP-20260927-00001", encryptPii("Juan Dela Cruz"), received),
        _makePendingRow("PENDING_CONFIRM", "SMP-20260927-00002", "Maria Santos", received),  # legacy plaintext
    ]

    listing = await _service(_makePendingDb(2, rows))._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    first, second = listing["items"]
    assert first["sampleUid"] == "SMP-20260927-00001"
    assert (first["testType"], first["priorityLevel"], first["receivedAt"]) == ("Urinalysis", "ROUTINE", received)
    assert first["patientUid"] == "PAT-000001"
    assert "patientName" not in first and "patientName" not in second
    assert "Juan" not in str(listing) and "gAAAAA" not in str(listing)
    assert first["patientAge"] is not None and first["patientSex"] == "FEMALE"
    assert listing["total"] == 2


@pytest.mark.asyncio
async def test_pendingReturnedRowCarriesTheLatestReason():
    row = _makePendingRow("RETURNED_FOR_CORRECTION", "SMP-1", None, datetime.now(UTC))
    ret = MagicMock(spec=ResultReturn)
    ret.resultId = row[0].resultId
    ret.reason = "Recount RBC"

    listing = await _service(_makePendingDb(1, [row], returns=[ret]))._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    assert listing["items"][0]["returnReason"] == "Recount RBC"


@pytest.mark.asyncio
async def test_totalUsesACountQueryInsteadOfLoadingEveryRow():
    db = _makePendingDb(0, [])

    listing = await _service(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    assert listing == {"items": [], "total": 0, "page": 1, "pageSize": 20}
    assert "count(*)" in _sql(db.execute.await_args_list[0].args[0]).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort", "direction"), [("oldest", "ASC"), ("newest", "DESC")]
)
async def test_returnedFirstThenByReceivedTimeThenStableId(sort, direction):
    db = _makePendingDb(0, [])

    await _service(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20, sort=sort)

    sql = _sql(db.execute.await_args_list[1].args[0])
    orderBy = sql.split("ORDER BY", 1)[1]
    assert orderBy.index("CASE WHEN") < orderBy.index("received_at") < orderBy.index("result_id")
    assert f"specimens.received_at {direction} NULLS LAST" in orderBy


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PENDING_CONFIRM", "RETURNED_FOR_CORRECTION"])
async def test_statusFilterNarrowsBothTheListAndTheTotal(status):
    db = _makePendingDb(0, [])
    other = ({"PENDING_CONFIRM", "RETURNED_FOR_CORRECTION"} - {status}).pop()

    await _service(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20, status=status)

    for call in db.execute.await_args_list[:2]:  # count, then rows
        where = _sql(call.args[0]).split("WHERE", 1)[1].split("ORDER BY", 1)[0]
        assert status in where and other not in where


@pytest.mark.asyncio
async def test_withoutAFilterBothQueueStatusesAreListed():
    db = _makePendingDb(0, [])

    await _service(db)._queryPendingForMedtech(MEDTECH_ID, 1, 20)

    where = _sql(db.execute.await_args_list[1].args[0]).split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert ResultStatus.PENDING_CONFIRM in where and ResultStatus.RETURNED_FOR_CORRECTION in where
    assert ResultStatus.APPROVED not in where
