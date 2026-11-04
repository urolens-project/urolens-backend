"""Real-database tests — the receiving desk and sample labeling on a copy of live's schema.

These are website (receptionist) paths. On live, `specimen_rejections`, `sample_labels`
and `print_jobs` name their timestamp `rejected_at`, `generated_at` and `dispatched_at`,
not the `created_at` migration 0032 would have created (the tables existed before it,
so its `CREATE TABLE IF NOT EXISTS` skipped them). The models mapped `created_at` until
PR #110, so every insert and select of these rows failed on live; the mocked unit tests
never saw it. The drift check in test_migrations_db.py catches the column names; this
runs the services that write them.

- Rejecting a specimen at the desk stores the specimen as `REJECTED`, its
  `specimen_rejections` row and the audit row, and leaves the lab request waiting.
- A received specimen gets a label, a regenerated label supersedes it, printing uses
  the current label, and confirming it moves the specimen to `LABELED`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.print_job import PrintJob
from src.models.sample_label import SampleLabel
from src.models.specimen_rejection import SpecimenRejection
from src.schemas.specimen import SpecimenReceiveRequest
from src.services import labeling_service, specimen_service
from tests.db.conftest import requiresLiveCopy, seedLabRequest

pytestmark = requiresLiveCopy


@pytest.mark.asyncio
async def test_rejectingASpecimenAtTheReceivingDeskIsStored(db: AsyncSession) -> None:
    ids = await seedLabRequest(db)
    payload = SpecimenReceiveRequest(
        labRequestId=ids.labRequestId, visualCheckPassed=False,
        rejectionReason="WRONG_CONTAINER", freeTextNote="Leaking cup",
    )

    response = await specimen_service.receiveSpecimen(db, ids.receptionistId, payload)

    assert (response.status, response.sampleUid) == ("REJECTED", None)
    [rejection] = (await db.execute(
        select(SpecimenRejection).where(SpecimenRejection.specimenId == response.specimenId)
    )).scalars().all()
    assert (rejection.reasonCode, rejection.freeTextNote) == ("WRONG_CONTAINER", "Leaking cup")
    assert rejection.medtechId == ids.receptionistId
    assert rejection.createdAt is not None
    assert await db.scalar(
        text("SELECT status FROM lab_requests WHERE lab_request_id = :r"), {"r": ids.labRequestId}
    ) == "PENDING_SAMPLE"
    assert await db.scalar(
        text("SELECT count(*) FROM audit_logs WHERE event_type = 'SPECIMEN_REJECTED' AND entity_id = :s"),
        {"s": response.specimenId},
    ) == 1


@pytest.mark.asyncio
async def test_aReceivedSpecimenIsLabelledPrintedAndConfirmed(db: AsyncSession) -> None:
    ids = await seedLabRequest(db)
    received = await specimen_service.receiveSpecimen(
        db, ids.receptionistId, SpecimenReceiveRequest(labRequestId=ids.labRequestId, visualCheckPassed=True),
    )
    specimenId = received.specimenId

    first = await labeling_service.generateLabel(db, specimenId, ids.receptionistId)
    second = await labeling_service.generateLabel(db, specimenId, ids.receptionistId)
    printed = await labeling_service.printLabel(db, specimenId, ids.receptionistId)
    confirmed = await labeling_service.confirmLabelAffixed(db, specimenId, ids.receptionistId, False)

    assert (first.labelCount, second.labelCount) == (1, 2)
    labels = {
        label.labelId: label for label in (await db.execute(
            select(SampleLabel).where(SampleLabel.specimenId == specimenId)
        )).scalars().all()
    }
    await db.refresh(labels[first.labelId])
    await db.refresh(labels[second.labelId])
    assert labels[first.labelId].superseded
    assert not labels[second.labelId].superseded and labels[second.labelId].affixedConfirmed
    assert all(label.createdAt is not None for label in labels.values())

    assert printed.labelId == second.labelId
    job = await db.get(PrintJob, printed.printJobId)
    assert (job.status, job.createdAt is not None) == ("SENT", True)

    assert confirmed.updatedStatus == "LABELED"
    assert await db.scalar(
        text("SELECT status FROM specimens WHERE specimen_id = :s"), {"s": specimenId}
    ) == "LABELED"
