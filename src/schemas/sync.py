"""Mobile-client sync response shapes; see `services/sync_service.py`."""
from typing import Any

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer


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
    resultFields: TableChanges | None = None
    """The captured fields of the MedTech's results, as `SyncResultField` rows
    (camelCase, never the AI boxes). Only present while `FIELD_WORKFLOW_ENABLED`
    is on; with it off the key is left out, so the response is unchanged."""

    @model_serializer(mode="wrap")
    def _omitResultFieldsWhenOff(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Absent, not null, while the multi-field workflow is off.
        data = handler(self)
        if self.resultFields is None:
            data.pop("resultFields", None)
        return data


class SyncPullResponse(BaseModel):
    """Response body for `GET /api/v1/sync/pull`."""

    timestamp: str
    changes: SyncChanges
