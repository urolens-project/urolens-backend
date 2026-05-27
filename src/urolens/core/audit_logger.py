from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import cast, insert
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit_log import AuditLog
from .database import get_db


class AuditLogger:
    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        db: AsyncSession,
        detail_json: dict[str, Any] | None = None,
        request: Any = None,
    ) -> None:
        ip_address: str | None = None
        if request and hasattr(request, "client") and request.client:
            ip_address = request.client.host

        _entity_id = uuid.UUID(str(entity_id)) if not isinstance(entity_id, uuid.UUID) else entity_id
        _user_id = uuid.UUID(str(user_id)) if user_id and not isinstance(user_id, uuid.UUID) else user_id

        # Build values dict; exclude ip_address when None so PostgreSQL
        # uses its own NULL default — passing None through SQLAlchemy's Text
        # column type emits $n::VARCHAR which the inet column rejects.
        values: dict[str, Any] = {
            "log_id": uuid.uuid4(),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": _entity_id,
            "user_id": _user_id,
            "detail_json": detail_json or {},
        }
        if ip_address:
            values["ip_address"] = cast(ip_address, INET)

        stmt = insert(AuditLog).values(**values)
        await db.execute(stmt)


def get_audit_logger() -> AuditLogger:
    return AuditLogger()
