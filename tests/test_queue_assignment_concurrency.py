"""Real-concurrency test — queue_service.assignSpecimen (UROLENS-142).

`tests/test_queue_service.py` proves the race-closing *mechanisms*
(IntegrityError → SPECIMEN_ALREADY_ASSIGNED, rowcount==0 → same) with
deterministic, sequential mocks. That's necessary but not sufficient: it
never actually runs two calls at once, so it can't catch a bug where the
service's own Python-level logic (not the DB) introduces a check-then-act
window (e.g. reading `specimen.status` into a local variable and branching
on it later, after another coroutine has already changed it).

This file instead runs two `assignSpecimen` calls concurrently via
`asyncio.gather`, against a shared, `asyncio.Lock`-guarded fake "database"
that simulates exactly the two DB-level guarantees the real migration
provides:
  - the partial unique index (`ix_queue_assignments_one_active_per_specimen`)
    — modeled as "insert fails if another row already claimed this
    specimen_id", checked and applied atomically under the lock;
  - the conditional `UPDATE ... WHERE status = 'LABELED'` — modeled as
    "only flips status if it still matches the expected value," also
    atomic under the lock.

There is no real test database anywhere in this repo (Postgres-native
column types like the specimens/queue_assignments enums make a lightweight
SQLite substitute impractical here), so this fake is the closest available
proof that genuinely concurrent scheduling — not test-code ordering —
resolves to exactly one winner.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import UnprocessableException
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.queue import QueueAssignRequest
from src.services.queue_service import QueueService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000140")
MEDTECH_A_ID = uuid.UUID("00000000-0000-0000-0000-000000000141")
MEDTECH_B_ID = uuid.UUID("00000000-0000-0000-0000-000000000142")
RECEPTIONIST_1 = uuid.UUID("00000000-0000-0000-0000-000000000143")
RECEPTIONIST_2 = uuid.UUID("00000000-0000-0000-0000-000000000144")


class _NestedCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, excType, exc, tb):
        return False


class _SharedFakeDb:
    """The one "database" both fake sessions race against: a lock, the
    specimen's current status, and which assignment (if any) currently
    holds the partial-unique-index slot for a specimen.
    """

    def __init__(self, specimenStatus: str = "LABELED"):
        self.lock = asyncio.Lock()
        self.specimenStatus = specimenStatus
        self.activeAssignmentFor: dict[uuid.UUID, uuid.UUID] = {}
        self.callLog: list[str] = []


class _RacingFakeSession:
    """One simulated request's `AsyncSession`, sharing `_SharedFakeDb` with
    the other racing request. Every method awaits `asyncio.sleep(0)` first —
    a genuine event-loop yield point, so `asyncio.gather` actually
    interleaves the two sessions' operations instead of one running to
    completion before the other starts.
    """

    def __init__(self, shared: _SharedFakeDb, label: str, specimen, medtech):
        self._shared = shared
        self._label = label
        self._specimen = specimen
        self._medtech = medtech
        self._pendingSpecimenId: uuid.UUID | None = None

    async def get(self, model, _id):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:get:{model.__name__}")
        if model is Specimen:
            return self._specimen
        if model is User:
            return self._medtech
        return None

    def add(self, _obj):
        pass

    async def flush(self, objs):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:flush")
        for obj in objs:
            async with self._shared.lock:
                if self._shared.activeAssignmentFor.get(obj.specimenId) is not None:
                    raise IntegrityError(
                        "INSERT INTO queue_assignments ...",
                        {},
                        Exception(
                            'duplicate key value violates unique constraint '
                            '"ix_queue_assignments_one_active_per_specimen"'
                        ),
                    )
                obj.assignmentId = uuid.uuid4()
                self._shared.activeAssignmentFor[obj.specimenId] = obj.assignmentId
                self._pendingSpecimenId = obj.specimenId

    def begin_nested(self):
        return _NestedCtx()

    async def execute(self, stmt):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:execute:{'select' if stmt.is_select else 'update'}")
        result = MagicMock()
        if stmt.is_select:
            # The pre-check: in the real race window nothing is visible yet
            # to either side at this point — both reach here before either
            # has inserted.
            result.scalar_one_or_none.return_value = None
            return result

        params = stmt.compile().params
        async with self._shared.lock:
            if (
                self._specimen.specimenId == params["specimen_id_1"]
                and self._shared.specimenStatus == params["status_1"]
            ):
                self._shared.specimenStatus = params["status"]
                self._specimen.status = params["status"]
                result.rowcount = 1
            else:
                result.rowcount = 0
        return result

    async def commit(self):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:commit")

    async def rollback(self):
        await asyncio.sleep(0)
        self._shared.callLog.append(f"{self._label}:rollback")
        if self._pendingSpecimenId is not None:
            self._shared.activeAssignmentFor.pop(self._pendingSpecimenId, None)
            self._pendingSpecimenId = None


def _makeService(db):
    notificationService = MagicMock()
    notificationService.notify = AsyncMock()
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return QueueService(
        db=MagicMock(),
        auditLogger=auditLogger,
        _notificationService=notificationService,
        sqlalchemyDb=db,
    )


def _makeRequest():
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    return request


@pytest.mark.asyncio
async def test_twoConcurrentAssignmentsOnSameSpecimenOneWinsOneGetsAlreadyAssigned():
    shared = _SharedFakeDb(specimenStatus="LABELED")

    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = "LABELED"
    specimen.sampleUid = "SMP-RACE-00001"

    medtechA = MagicMock(spec=User)
    medtechA.userId = MEDTECH_A_ID
    medtechA.role = "MEDTECH"
    medtechA.isActive = True

    medtechB = MagicMock(spec=User)
    medtechB.userId = MEDTECH_B_ID
    medtechB.role = "MEDTECH"
    medtechB.isActive = True

    dbA = _RacingFakeSession(shared, "A", specimen, medtechA)
    dbB = _RacingFakeSession(shared, "B", specimen, medtechB)

    serviceA = _makeService(dbA)
    serviceB = _makeService(dbB)

    dataA = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_A_ID)
    dataB = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_B_ID)

    resultA, resultB = await asyncio.gather(
        serviceA.assignSpecimen(dataA, RECEPTIONIST_1, _makeRequest()),
        serviceB.assignSpecimen(dataB, RECEPTIONIST_2, _makeRequest()),
        return_exceptions=True,
    )

    outcomes = [resultA, resultB]
    successes = [o for o in outcomes if not isinstance(o, Exception)]
    failures = [o for o in outcomes if isinstance(o, Exception)]

    # Proof this was genuinely interleaved, not "A ran fully, then B ran":
    # both sides' pre-write calls appear before either side's flush.
    firstFlushIndex = next(i for i, e in enumerate(shared.callLog) if e.endswith(":flush"))
    assert any(e.startswith("A:") for e in shared.callLog[:firstFlushIndex])
    assert any(e.startswith("B:") for e in shared.callLog[:firstFlushIndex])

    assert len(successes) == 1, f"exactly one concurrent assignment must win, got: {outcomes}"
    assert len(failures) == 1, f"the other must fail cleanly, not silently succeed too, got: {outcomes}"

    failure = failures[0]
    assert isinstance(failure, UnprocessableException), (
        f"the loser must get the domain exception, not a raw error: {failure!r}"
    )
    assert failure.errorCode == "SPECIMEN_ALREADY_ASSIGNED"
    assert failure.status_code == 422

    # The DB itself ends up with exactly one active assignment for the
    # specimen, and the specimen transitioned to ASSIGNED exactly once.
    assert len(shared.activeAssignmentFor) == 1
    assert shared.specimenStatus == "ASSIGNED"


@pytest.mark.asyncio
async def test_twoConcurrentAssignmentsToDifferentSpecimensBothSucceed():
    """Sanity check on the fake itself: the lock must guard per-specimen
    contention, not serialize unrelated assignments into a false failure.
    """
    specimenIdB = uuid.uuid4()

    specimenA = MagicMock(spec=Specimen)
    specimenA.specimenId = SPECIMEN_ID
    specimenA.status = "LABELED"
    specimenA.sampleUid = "SMP-RACE-A"

    specimenB = MagicMock(spec=Specimen)
    specimenB.specimenId = specimenIdB
    specimenB.status = "LABELED"
    specimenB.sampleUid = "SMP-RACE-B"

    medtechA = MagicMock(spec=User)
    medtechA.userId = MEDTECH_A_ID
    medtechA.role = "MEDTECH"
    medtechA.isActive = True

    # Each fake session models a distinct specimen's underlying "row" —
    # unrelated to each other, both should succeed independently.
    class _SharedFakeDbPerSpecimen(_SharedFakeDb):
        def __init__(self):
            super().__init__()
            self.statusBySpecimen: dict[uuid.UUID, str] = {
                SPECIMEN_ID: "LABELED",
                specimenIdB: "LABELED",
            }

    sharedMulti = _SharedFakeDbPerSpecimen()

    class _PerSpecimenSession(_RacingFakeSession):
        async def execute(self, stmt):
            await asyncio.sleep(0)
            result = MagicMock()
            if stmt.is_select:
                result.scalar_one_or_none.return_value = None
                return result
            params = stmt.compile().params
            async with self._shared.lock:
                sid = params["specimen_id_1"]
                if sharedMulti.statusBySpecimen.get(sid) == params["status_1"]:
                    sharedMulti.statusBySpecimen[sid] = params["status"]
                    result.rowcount = 1
                else:
                    result.rowcount = 0
            return result

    dbA = _PerSpecimenSession(sharedMulti, "A", specimenA, medtechA)
    dbB = _PerSpecimenSession(sharedMulti, "B", specimenB, medtechA)

    serviceA = _makeService(dbA)
    serviceB = _makeService(dbB)

    dataA = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_A_ID)
    dataB = QueueAssignRequest(specimenId=specimenIdB, medtechId=MEDTECH_A_ID)

    resultA, resultB = await asyncio.gather(
        serviceA.assignSpecimen(dataA, RECEPTIONIST_1, _makeRequest()),
        serviceB.assignSpecimen(dataB, RECEPTIONIST_2, _makeRequest()),
        return_exceptions=True,
    )

    assert not isinstance(resultA, Exception), resultA
    assert not isinstance(resultB, Exception), resultB
