import json
from datetime import datetime, timezone

from app.config import ZERO_UUID
from app.db.supabase import supabase


class AuditLogger:
    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        user_id: str,
        detail_json: dict | None = None,
        db=None,
        request=None,
    ) -> None:
        ip_address = "unknown"
        if request and request.client:
            ip_address = request.client.host

        entry = {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "user_id": str(user_id) if user_id else ZERO_UUID,
            "ip_address": ip_address,
            "detail_json": json.dumps(detail_json) if detail_json else None,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        await supabase.table("audit_logs").insert(entry).execute()
