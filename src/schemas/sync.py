"""Mobile-client sync response shapes; see `services/sync_service.py`."""
from typing import Any

from pydantic import BaseModel


class TableChanges(BaseModel):
    """Created/updated rows for one table in a sync response. Exactly one of
    `created`/`updated` is populated per `sync_service.pull`'s full-vs-delta rule.
    """

    created: list[Any]
    updated: list[Any]
    deleted: list[str] = []
    """IDs the phone should remove (UROLENS-236): samples that aged out of the
    sync window or are no longer assigned to the MedTech. Only filled on a
    delta sync; existing app versions ignore it."""


class SyncChanges(BaseModel):
    """Per-table `TableChanges` for the tables the mobile client syncs."""

    specimens: TableChanges
    queueAssignments: TableChanges
    analysisResults: TableChanges
    manualOverrides: TableChanges
    """Corrections on the MedTech's results, from any author (UROLENS-227)."""


class SyncPullResponse(BaseModel):
    """Response body for `GET /api/v1/sync/pull`."""

    timestamp: str
    changes: SyncChanges
