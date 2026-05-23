from typing import Any

from pydantic import BaseModel


class TableChanges(BaseModel):
    created: list[Any]
    updated: list[Any]


class SyncChanges(BaseModel):
    specimens: TableChanges
    queue_assignments: TableChanges
    analysis_results: TableChanges


class SyncPullResponse(BaseModel):
    timestamp: str
    changes: SyncChanges
