"""Mobile-client sync response shapes; see `services/sync_service.py`."""
from typing import Any

from pydantic import BaseModel


class TableChanges(BaseModel):
    """Created/updated rows for one table in a sync response. Exactly one of
    `created`/`updated` is populated per `sync_service.pull`'s full-vs-delta rule.
    """

    created: list[Any]
    updated: list[Any]


class SyncChanges(BaseModel):
    """Per-table `TableChanges` for the tables the mobile client syncs."""

    specimens: TableChanges
    queueAssignments: TableChanges
    analysisResults: TableChanges


class SyncPullResponse(BaseModel):
    """Response body for `GET /api/v1/sync/pull`."""

    timestamp: str
    changes: SyncChanges
