"""Suite-wide fixtures and helpers.

The login rate limiter (`src.core.rate_limit`) keeps its counts in process
memory, so without a reset one test's login attempts would count against
the next test's — clear it around every test.

`makeSyncDb` is a session mock for `sync_service.pull`, shared by the sync
tests; `syncSpecimen` / `syncResult` build the rows it returns.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select
from sqlalchemy.engine import make_url

from src.core import rate_limit
from src.core.config import settings
from src.models.analysis_result import AnalysisResult
from src.models.manual_override import ManualOverride
from src.models.queue_assignment import QueueAssignment
from src.models.result_approval import ResultApproval
from src.models.result_field import ResultField
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen

_LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture(scope="session", autouse=True)
def refuseNonLocalDatabase() -> None:
    """Stop the whole run if the app's `DATABASE_URL` is not on this machine.

    `.env` names the live database on a dev machine. Environment variables win
    over it (`load_dotenv(override=False)`), but with none set the live URL is
    what `settings` holds. CI's placeholder is on localhost, so it passes. The
    message never prints the URL or its host, since both come from `.env`.
    """
    host = make_url(settings.databaseUrl).host
    if host not in _LOCAL_DB_HOSTS:
        pytest.exit(
            "Refusing to run: DATABASE_URL does not point at a local database. Set DATABASE_URL "
            "to a placeholder or a scratch database on localhost (see .github/workflows/ci.yml) "
            "before running the tests.",
            returncode=2,
        )


SYNC_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


def syncSpecimen(**fields: object) -> Specimen:
    """A specimen of `SYNC_MEDTECH_ID`'s, as the sync query loads it."""
    defaults: dict[str, object] = {
        "specimenId": uuid.uuid4(),
        "sampleUid": "SMP-20260930-00001",
        "patientUid": "PAT-000001",
        "testType": "Urinalysis - Routine",
        "status": "PROCESSING",
        "priorityLevel": "ROUTINE",
        "receivedAt": datetime(2026, 9, 29, 8, 0, tzinfo=UTC),
        "updatedAt": datetime(2026, 9, 29, 9, 0, tzinfo=UTC),
        "medtechId": SYNC_MEDTECH_ID,
    }
    return Specimen(**{**defaults, **fields})


def syncResult(specimen: Specimen, **fields: object) -> AnalysisResult:
    """An analysis result for `specimen`, as the sync query loads it."""
    defaults: dict[str, object] = {
        "resultId": uuid.uuid4(),
        "specimenId": specimen.specimenId,
        "status": "PENDING_CONFIRM",
        "aiFindings": {"erythrocytes": 2},
        "flaggedAnomalies": {"erythrocytes": 2},
        "particleClasses": {},
        "smartDiagnosisUnavailable": False,
        "modelVersion": "mvp-v1.0",
        "updatedAt": datetime(2026, 9, 29, 9, 0, tzinfo=UTC),
    }
    return AnalysisResult(**{**defaults, **fields})


def makeSyncDb(
    rows: list[tuple[Specimen, AnalysisResult | None]] | None = None,
    assignments: list[QueueAssignment] | None = None,
    reasons: list[tuple] | None = None,
    approvals: list[tuple] | None = None,
    overrides: list[ManualOverride] | None = None,
    removed: list[tuple] | None = None,
    removedAssignmentIds: list[uuid.UUID] | None = None,
    removedOverrideIds: list[uuid.UUID] | None = None,
    fields: list[ResultField] | None = None,
    removedFieldIds: list[uuid.UUID] | None = None,
) -> AsyncMock:
    """A session answering each of `sync_service.pull`'s queries.

    `rows`: (specimen, result) pairs from the window query. `reasons` /
    `approvals`: (resultId, value) rows. `removed`: (specimenId, resultId)
    rows from the removals query, with the assignment and override IDs of
    those samples. `fields` / `removedFieldIds`: the multi-field queries, only
    run while `FIELD_WORKFLOW_ENABLED` is on. Statements are kept in
    `db.execute.await_args_list`.
    """

    def _execute(stmt: Select) -> MagicMock:
        first = stmt.column_descriptions[0]
        entity, name = first.get("entity"), first.get("name")
        answers: dict[tuple, tuple[str, list]] = {
            (Specimen, "Specimen"): ("all", rows or []),
            (Specimen, "specimenId"): ("all", removed or []),
            (QueueAssignment, "QueueAssignment"): ("scalars", assignments or []),
            (QueueAssignment, "assignmentId"): ("scalars", removedAssignmentIds or []),
            (ResultReturn, "resultId"): ("all", reasons or []),
            (ResultApproval, "resultId"): ("all", approvals or []),
            (ManualOverride, "ManualOverride"): ("scalars", overrides or []),
            (ManualOverride, "overrideId"): ("scalars", removedOverrideIds or []),
            (ResultField, "ResultField"): ("scalars", fields or []),
            (ResultField, "fieldId"): ("scalars", removedFieldIds or []),
        }
        kind, data = answers[(entity, name)]
        executeResult = MagicMock()
        if kind == "all":
            executeResult.all.return_value = data
        else:
            executeResult.scalars.return_value.all.return_value = data
        return executeResult

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


def syncQueries(db: AsyncMock, entity: type, name: str) -> list[Select]:
    """The statements `makeSyncDb`'s session ran for one query kind."""
    return [
        c.args[0] for c in db.execute.await_args_list
        if (c.args[0].column_descriptions[0].get("entity"), c.args[0].column_descriptions[0].get("name"))
        == (entity, name)
    ]


# Modules written for the single-image workflow that don't pin the flag off
# themselves (the upload and override modules carry their own autouse fixture).
# They assert the responses and queries that exist while `FIELD_WORKFLOW_ENABLED`
# is off. The suite is run in both modes, so these are pinned to off. A test that wants the flag on sets it
# itself with `monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)`,
# which runs after this fixture and wins. Remove a module from the set when its
# tests are moved to the multi-field workflow.
_SINGLE_IMAGE_MODULES = frozenset({
    "test_review_confirm_http",
    "test_smart_diagnosis",
    "test_result_review_service",
    "test_result_confirmation_service",
    "test_result_review_and_annotate",
    "test_assigned_queue",
    "test_phi_access_logging",
    "test_specimen_access",
    "test_w0_corrected_counts_and_audit",
})


@pytest.fixture(autouse=True)
def singleImageWorkflow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.module.__name__.rsplit(".", 1)[-1] in _SINGLE_IMAGE_MODULES:
        monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)


@pytest.fixture(autouse=True)
def clearLoginRateLimits():
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
    yield
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
