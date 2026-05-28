from __future__ import annotations

import uuid
from typing import Any

from app.db.supabase import supabase


class AuditLogger:
    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        db: Any = None,          # kept for backward compatibility, ignored
        detail_json: dict[str, Any] | None = None,
        request: Any = None,
    ) -> None:
        ip_address: str | None = None
        if request and hasattr(request, "client") and request.client:
            ip_address = request.client.host

        try:
            await supabase.table("audit_logs").insert({
                "log_id": str(uuid.uuid4()),
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "user_id": str(user_id) if user_id else None,
                "detail_json": detail_json or {},
                "ip_address": ip_address,
            }).execute()
        except Exception:
            # Audit must never break the main transaction
            pass


def get_audit_logger() -> AuditLogger:
    return AuditLogger()
