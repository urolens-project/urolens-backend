"""In-app notification and push-token request/response shapes; see
`api/notifications.py`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Expo push tokens look like `ExponentPushToken[...]` (or the newer
# `ExpoPushToken[...]`); anything else can't be delivered through Expo.
EXPO_TOKEN_PREFIXES = ("ExponentPushToken[", "ExpoPushToken[")


class NotificationOut(BaseModel):
    """Response shape for a single notification row."""

    notificationId: uuid.UUID
    message: str
    notificationType: str
    entityId: uuid.UUID | None
    isRead: bool
    createdAt: datetime

    model_config = {"from_attributes": True}


class NotificationUnreadCountResponse(BaseModel):
    """How many of the caller's notifications are unread — the bell badge (UROLENS-248)."""

    unreadCount: int


class PushTokenRequest(BaseModel):
    """Request body for registering a device's Expo push token."""

    token: str = Field(..., min_length=1, max_length=255)

    @field_validator("token")
    @classmethod
    def requireExpoToken(cls, value: str) -> str:
        """Refuse anything that isn't an Expo push token (UROLENS-248).

        Raises:
            ValueError: if the token doesn't have an Expo token's shape (422).
        """
        value = value.strip()
        if not (value.startswith(EXPO_TOKEN_PREFIXES) and value.endswith("]")):
            raise ValueError("token must be an Expo push token")
        return value
