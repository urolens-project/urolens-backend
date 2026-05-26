# urolens-backend/schemas/push_token_schema.py

from pydantic import BaseModel, Field


class PushTokenRegisterRequest(BaseModel):
    """
    Request body for POST /api/v1/users/me/push-token.
    Sent by the mobile client after Expo token registration.
    """

    expo_push_token: str = Field(
        ...,
        description="Expo push token in the format 'ExponentPushToken[xxxxxx]'",
        examples=["ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"],
    )