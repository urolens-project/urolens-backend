"""In-app notification and push-token request/response shapes; see
`api/notifications.py`."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    """Response shape for a single notification row."""

    notification_id: uuid.UUID
    message: str
    notification_type: str
    entity_id: uuid.UUID | None
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PushTokenRequest(BaseModel):
    """Request body for registering a device's Expo push token."""

    token: str
