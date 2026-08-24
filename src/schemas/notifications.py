"""In-app notification and push-token request/response shapes; see
`api/notifications.py`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    """Response shape for a single notification row."""

    notificationId: uuid.UUID
    message: str
    notificationType: str
    entityId: uuid.UUID | None
    isRead: bool
    createdAt: datetime

    model_config = {"from_attributes": True}


class PushTokenRequest(BaseModel):
    """Request body for registering a device's Expo push token."""

    token: str
