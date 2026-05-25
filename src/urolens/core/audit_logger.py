from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends
from supabase import AsyncClient

from app.db.supabase import get_supabase


class AuditLogger:
    def __init__(self, db: AsyncClient) -> None:
        self._db = db

    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        detail_json: dict[str, Any] | None = None,
        request: Any = None,
    ) -> None:
        ip_address: str | None = None
        if request and hasattr(request, "client") and request.client:
            ip_address = request.client.host

        await self._db.table("audit_logs").insert({
            "log_id": str(uuid.uuid4()),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "user_id": str(user_id) if user_id else None,
            "ip_address": ip_address,
            "detail_json": detail_json,
        }).execute()


def get_audit_logger(db: AsyncClient = Depends(get_supabase)) -> AuditLogger:
    return AuditLogger(db=db)
