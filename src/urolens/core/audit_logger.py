from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..models.audit_log import AuditLog


class AuditLogger:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        detail_json: dict[str, Any] | None = None,
        db: AsyncSession | None = None,
        request: Any = None,
    ) -> None:
        session = db or self._db

        ip_address: str | None = None
        if request and hasattr(request, "client") and request.client:
            ip_address = request.client.host

        entry = AuditLog(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=uuid.UUID(str(entity_id)) if not isinstance(entity_id, uuid.UUID) else entity_id,
            user_id=uuid.UUID(str(user_id)) if user_id and not isinstance(user_id, uuid.UUID) else user_id,
            ip_address=ip_address,
            detail_json=detail_json,
        )
        session.add(entry)
        await session.flush()


def get_audit_logger(db: AsyncSession = Depends(get_db)) -> AuditLogger:
    return AuditLogger(db=db)
