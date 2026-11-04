"""Process-level status shapes; see `main.rootHealthCheck`."""
from pydantic import BaseModel


class HealthCheckResponse(BaseModel):
    """Response body for `GET /` — a liveness check, not a dependency probe."""

    status: str
    service: str


class ReadinessResponse(BaseModel):
    """Response body for `GET /health/ready`; both true is ready (200), else 503."""

    db: bool
    model: bool
