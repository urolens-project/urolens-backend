from pydantic import BaseModel


class DiscardImageResponse(BaseModel):
    image_id: str
    status: str
