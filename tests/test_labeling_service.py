"""Unit tests — labeling_service (UROLENS-141, blocks 1 and 2).

Block 1:
  - item 1: generateLabel no longer creates a PrintJob; the new printLabel
    (POST .../label/print) does.
  - item 2: confirmLabelAffixed requires specimen.status == RECEIVED, with
    or without offlineOverride.
  - item 3: the newest, non-superseded label is picked deterministically
    (order_by createdAt desc), and generateLabel marks prior labels
    superseded before inserting a new one.
  - item 4: labelCount is returned by generateLabel and reflects regenerates.

Block 2:
  - item 5: search-received no longer returns/searches by patientName, `q`
    is matched via an escaped ILIKE at the DB level (min-length enforcement
    is a route-level Query(min_length=3) concern — see
    tests/integration/test_labeling_search_validation.py for that).
  - item 6: generateLabel/printLabel/confirmLabelAffixed each write an
    AuditLogger entry.

This suite mocks `db` (an `AsyncSession`) entirely — no real test database
exists anywhere in this repo (same convention as test_specimen_receive.py).
Where the fix is really "the SQL query shape is now correct" (item 3's
ordering/filtering, item 5's escaping), the test inspects the compiled
statement text passed to the mocked `db.execute`, since a mock can't
actually apply a WHERE/ORDER BY/ESCAPE. `AuditLogger` is patched in every
success-path test (not just the ones asserting on it) so no test attempts a
real Supabase network call.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import SpecimenNotFoundError, UnprocessableException
from src.models.print_job import PrintJob
from src.models.sample_label import SampleLabel
from src.models.specimen import Specimen
from src.services import labeling_service

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
OPERATOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
LABEL_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")


def _makeReceivedSpecimen(status: str = "RECEIVED") -> MagicMock:
    specimen = MagicMock(spec=Specimen)
    specimen.status = status
    specimen.patientName = "encrypted-name"
    specimen.patientUid = "PAT-000001"
    specimen.sampleUid = "SMP-000001"
    specimen.testType = "URINALYSIS"
    return specimen


# ── item 1: generateLabel no longer writes a PrintJob ───────────────────────


@pytest.mark.asyncio
async def test_generateLabelDoesNotCreatePrintJob():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    updateResult = MagicMock()
    countResult = MagicMock()
    countResult.scalar_one.return_value = 1
    db.execute = AsyncMock(side_effect=[updateResult, countResult])

    async def _flush(objs):
        for obj in objs:
            obj.labelId = LABEL_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.add = MagicMock()
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"), patch(
        "src.services.labeling_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        result = await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)

    addedTypeNames = [type(call.args[0]).__name__ for call in db.add.call_args_list]
    assert "PrintJob" not in addedTypeNames
    assert addedTypeNames == ["SampleLabel"]
    assert result.printJobId is None
    assert result.labelCount == 1


# ── item 1: the new print endpoint creates the PrintJob ─────────────────────


@pytest.mark.asyncio
async def test_printLabelCreatesPrintJobWithStatusSent():
    specimen = _makeReceivedSpecimen()
    label = MagicMock(spec=SampleLabel)
    label.labelId = LABEL_ID

    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    labelQueryResult = MagicMock()
    labelQueryResult.scalars.return_value.first.return_value = label
    db.execute = AsyncMock(return_value=labelQueryResult)

    async def _flush(objs):
        for obj in objs:
            obj.printJobId = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    db.add = MagicMock()
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        result = await labeling_service.printLabel(db, SPECIMEN_ID, OPERATOR_ID)

    assert db.add.call_count == 1
    addedPrintJob = db.add.call_args[0][0]
    assert isinstance(addedPrintJob, PrintJob)
    assert addedPrintJob.status == "SENT"
    assert addedPrintJob.labelId == LABEL_ID
    assert result.status == "SENT"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_printLabelRaisesLabelNotFoundWhenNoLabelExists():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    labelQueryResult = MagicMock()
    labelQueryResult.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=labelQueryResult)
    db.add = MagicMock()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excInfo:
        await labeling_service.printLabel(db, SPECIMEN_ID, OPERATOR_ID)

    assert excInfo.value.status_code == 400
    assert excInfo.value.errorCode == "LABEL_NOT_FOUND"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_printLabelRaisesNotFoundForUnknownSpecimen():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(SpecimenNotFoundError):
        await labeling_service.printLabel(db, SPECIMEN_ID, OPERATOR_ID)


# ── item 2: confirm requires specimen.status == RECEIVED ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("offlineOverride", [False, True])
@pytest.mark.parametrize("badStatus", ["REJECTED", "LABELED", "ASSIGNED", "PROCESSING"])
async def test_confirmNonReceivedSpecimenRaisesSpecimenNotReceived(badStatus, offlineOverride):
    specimen = _makeReceivedSpecimen(status=badStatus)
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    db.execute = AsyncMock()

    with pytest.raises(UnprocessableException) as excInfo:
        await labeling_service.confirmLabelAffixed(db, SPECIMEN_ID, OPERATOR_ID, offlineOverride)

    assert excInfo.value.errorCode == "SPECIMEN_NOT_RECEIVED"
    # The status check fires before any label lookup — offlineOverride skips
    # the printer requirement, not this check.
    db.execute.assert_not_awaited()


# ── item 3: newest, non-superseded label is picked deterministically ────────


@pytest.mark.asyncio
async def test_currentLabelForQueriesNewestNonSupersededLabel():
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=result)

    await labeling_service._currentLabelFor(db, SPECIMEN_ID)

    stmt = db.execute.call_args[0][0]
    compiled = str(stmt)
    assert "ORDER BY sample_labels.created_at DESC" in compiled
    assert "sample_labels.superseded IS false" in compiled


@pytest.mark.asyncio
async def test_confirmLabelAffixedConfirmsWhicheverLabelTheQueryReturned():
    """Plumbing check: confirmLabelAffixed must act on exactly the label
    object `_currentLabelFor`'s query returns — not re-select or index into
    a different row.
    """
    specimen = _makeReceivedSpecimen()
    newestLabel = MagicMock(spec=SampleLabel)
    newestLabel.affixedConfirmed = False
    newestLabel.affixedAt = None

    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    labelQueryResult = MagicMock()
    labelQueryResult.scalars.return_value.first.return_value = newestLabel
    db.execute = AsyncMock(return_value=labelQueryResult)
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.confirmLabelAffixed(db, SPECIMEN_ID, OPERATOR_ID, False)

    assert newestLabel.affixedConfirmed is True
    assert newestLabel.affixedAt is not None
    assert specimen.status == "LABELED"


@pytest.mark.asyncio
async def test_generateLabelSupersedesPriorLabelsBeforeInsertingNew():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    updateResult = MagicMock()
    countResult = MagicMock()
    countResult.scalar_one.return_value = 2
    db.execute = AsyncMock(side_effect=[updateResult, countResult])
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.labelId = LABEL_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"), patch(
        "src.services.labeling_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)

    supersedeStmt = db.execute.call_args_list[0].args[0]
    compiled = str(supersedeStmt)
    assert compiled.startswith("UPDATE sample_labels SET superseded")
    assert "sample_labels.superseded IS false" in compiled
    # The supersede write happens before the new label is added.
    assert db.add.call_args_list[0].args[0].__class__.__name__ == "SampleLabel"


# ── item 4: labelCount reflects generate + regenerates ───────────────────────


@pytest.mark.asyncio
async def test_labelCountAfterGenerateAndTwoRegenerates():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    def _countResult(n: int) -> MagicMock:
        result = MagicMock()
        result.scalar_one.return_value = n
        return result

    db.execute = AsyncMock(
        side_effect=[
            MagicMock(),  # supersede update, generate #1
            _countResult(1),
            MagicMock(),  # supersede update, regenerate #2
            _countResult(2),
            MagicMock(),  # supersede update, regenerate #3
            _countResult(3),
        ]
    )
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.labelId = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"), patch(
        "src.services.labeling_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        first = await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)
        second = await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)
        third = await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)

    assert first.labelCount == 1
    assert second.labelCount == 2
    assert third.labelCount == 3
    # labelCount - 1 == "times regenerated"
    assert third.labelCount - 1 == 2


@pytest.mark.asyncio
async def test_searchReceivedSpecimensIncludesLabelCount():
    specimen = _makeReceivedSpecimen()
    specimen.specimenId = SPECIMEN_ID

    # The DB itself now does the filtering (ILIKE), so the mock's row set
    # already represents "what the query matched" — no Python-side re-match.
    specimenQueryResult = MagicMock()
    specimenQueryResult.scalars.return_value.all.return_value = [specimen]
    countQueryResult = MagicMock()
    countQueryResult.all.return_value = [(SPECIMEN_ID, 3)]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[specimenQueryResult, countQueryResult])

    items = await labeling_service.searchReceivedSpecimens(db, "PAT-0000")

    assert len(items) == 1
    assert items[0].labelCount == 3


# ── item 5: no patientName in search results, min-length/escaping ───────────


@pytest.mark.asyncio
async def test_searchReceivedSpecimensResponseHasNoPatientNameField():
    specimen = _makeReceivedSpecimen()
    specimen.specimenId = SPECIMEN_ID

    specimenQueryResult = MagicMock()
    specimenQueryResult.scalars.return_value.all.return_value = [specimen]
    countQueryResult = MagicMock()
    countQueryResult.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[specimenQueryResult, countQueryResult])

    items = await labeling_service.searchReceivedSpecimens(db, "PAT-0000")

    assert not hasattr(items[0], "patientName")
    assert items[0].patientUid == specimen.patientUid
    assert items[0].sampleUid == specimen.sampleUid


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("50%", "50\\%"),
        ("a_b", "a\\_b"),
        ("100%_off", "100\\%\\_off"),
        ("back\\slash", "back\\\\slash"),
        ("plain", "plain"),
    ],
)
def test_escapeLikePatternEscapesWildcardsLiterally(raw, expected):
    assert labeling_service._escapeLikePattern(raw) == expected


@pytest.mark.asyncio
async def test_searchReceivedSpecimensEscapesWildcardsInTheCompiledQuery():
    """A bare `%` or `_` in `q` must not act as a SQL wildcard — the compiled
    statement should carry the escaped literal plus an ESCAPE clause, not a
    raw `%` that would match everything up to `_MAX_SEARCH_RESULTS`.
    """
    specimenQueryResult = MagicMock()
    specimenQueryResult.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=specimenQueryResult)

    await labeling_service.searchReceivedSpecimens(db, "50%_off")

    stmt = db.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "%50\\%\\_off%" in compiled
    assert "ESCAPE '\\'" in compiled


# ── item 6: audit entries for generate / print / confirm ────────────────────


@pytest.mark.asyncio
async def test_generateLabelWritesAuditEntryWithLabelCountAndRegeneratedFlag():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    countResult = MagicMock()
    countResult.scalar_one.return_value = 2  # a regenerate: 2nd label overall
    db.execute = AsyncMock(side_effect=[MagicMock(), countResult])

    async def _flush(objs):
        for obj in objs:
            obj.labelId = LABEL_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.add = MagicMock()
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"), patch(
        "src.services.labeling_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)

    mockAuditCls.return_value.record.assert_awaited_once()
    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["eventType"] == "LABEL_GENERATED"
    assert kwargs["entityType"] == "specimen"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == OPERATOR_ID
    assert kwargs["detailJson"]["labelId"] == str(LABEL_ID)
    assert kwargs["detailJson"]["labelCount"] == 2
    assert kwargs["detailJson"]["regenerated"] is True


@pytest.mark.asyncio
async def test_generateLabelAuditEntryRegeneratedFlagFalseOnFirstGenerate():
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    countResult = MagicMock()
    countResult.scalar_one.return_value = 1
    db.execute = AsyncMock(side_effect=[MagicMock(), countResult])

    async def _flush(objs):
        for obj in objs:
            obj.labelId = LABEL_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.add = MagicMock()
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"), patch(
        "src.services.labeling_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.generateLabel(db, SPECIMEN_ID, OPERATOR_ID)

    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["detailJson"]["regenerated"] is False


@pytest.mark.asyncio
async def test_printLabelWritesAuditEntryWithOperatorAsActor():
    specimen = _makeReceivedSpecimen()
    label = MagicMock(spec=SampleLabel)
    label.labelId = LABEL_ID
    printJobId = uuid.uuid4()

    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    labelQueryResult = MagicMock()
    labelQueryResult.scalars.return_value.first.return_value = label
    db.execute = AsyncMock(return_value=labelQueryResult)
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.printJobId = printJobId

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.printLabel(db, SPECIMEN_ID, OPERATOR_ID)

    mockAuditCls.return_value.record.assert_awaited_once()
    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["eventType"] == "LABEL_PRINTED"
    assert kwargs["entityType"] == "specimen"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == OPERATOR_ID
    assert kwargs["detailJson"]["printJobId"] == str(printJobId)
    assert kwargs["detailJson"]["labelId"] == str(LABEL_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("offlineOverride", [False, True])
async def test_confirmLabelAffixedWritesAuditEntryWithOfflineOverrideFlag(offlineOverride):
    specimen = _makeReceivedSpecimen()
    label = MagicMock(spec=SampleLabel)
    label.labelId = LABEL_ID
    label.affixedConfirmed = False
    label.affixedAt = None

    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    labelQueryResult = MagicMock()
    # offlineOverride=True is only exercised when a label already exists here
    # too — this test isn't re-proving the "create one on the fly" path,
    # just that the audit entry's offlineOverride flag reflects the caller's
    # argument correctly either way.
    labelQueryResult.scalars.return_value.first.return_value = label
    db.execute = AsyncMock(return_value=labelQueryResult)
    db.commit = AsyncMock()

    with patch("src.services.labeling_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await labeling_service.confirmLabelAffixed(db, SPECIMEN_ID, OPERATOR_ID, offlineOverride)

    mockAuditCls.return_value.record.assert_awaited_once()
    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["eventType"] == "LABEL_CONFIRMED"
    assert kwargs["entityType"] == "specimen"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == OPERATOR_ID
    assert kwargs["detailJson"]["labelId"] == str(LABEL_ID)
    assert kwargs["detailJson"]["offlineOverride"] is offlineOverride
