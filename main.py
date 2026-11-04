import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(override=False)

# import src.models  # noqa: F401 — registers all SQLAlchemy models before first query

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.api import (
    auth,
    patient_auth,
    patient_portal,
    patients,
    physician,
    queue,
)
from src.api.fields import router as fields_router
from src.api.image import router as images_router
from src.api.lab_requests import router as lab_requests_router
from src.api.labeling import router as labeling_router
from src.api.notifications import router as notifications_router  # Epic 8
from src.api.result_releasing import router as result_releasing_router

# Mobile developer routers
from src.api.results import router as results_router

# Web developer routers
from src.api.specimens import router as src_specimens_router
from src.api.sync import router as sync_router
from src.core import engine_preload
from src.core.config import engineFileProblems, settings
from src.core.database import AsyncSessionLocal
from src.schemas.system import HealthCheckResponse, ReadinessResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Check the AI engine once, when the server starts.

    Each problem is logged as an error, so a broken upload has a cause in the logs
    from the start. With `FIELD_WORKFLOW_ENABLED` off the app still starts: only
    uploads and Smart Diagnosis need the engine. With it on, the server refuses to
    start, because the multi-field workflow relies on the pinned engine (its boxes,
    upright size and decimal Smart Diagnosis) and must not run on an old one.

    Raises:
        RuntimeError: if the flag is on and any engine problem was found.
    """
    problems = engineFileProblems(settings)
    for problem in problems:
        logger.error("AI engine: %s", problem)
    if problems and settings.fieldWorkflowEnabled:
        raise RuntimeError(
            "FIELD_WORKFLOW_ENABLED is on but the AI engine is not ready; fix the "
            "'AI engine:' errors above (or turn the flag off) and restart."
        )
    # Load the model now rather than on the first upload. Never raises: on failure
    # the app starts and /health/ready reports the model as not ready.
    await asyncio.to_thread(engine_preload.preloadEngine)
    yield


def parseCorsOrigins(raw: str | None) -> list[str]:
    """Split a comma-separated `CORS_ALLOWED_ORIGINS` value into origins.

    Whitespace around each origin is dropped, and so are empty entries. Unset or
    blank gives an empty list: no browser origin is allowed (the mobile app is not
    a browser and sends no `Origin`, so it is unaffected).
    """
    return [origin.strip() for origin in (raw or "").split(",") if origin.strip()]


app = FastAPI(title="UroLens LIS Engine", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=parseCorsOrigins(os.getenv("CORS_ALLOWED_ORIGINS")),
    # Auth is Bearer-token-in-header (see apiClient.ts / src.core.rbac), never
    # cookies — allow_credentials=True is not needed and must stay False.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ── Global error envelope ─────────────────────────────────────────────────────
# Every error response must follow {"error": {"code": "...", "message": "..."}}
# so the mobile apiClient can read response.data.error reliably.

_STATUS_TO_CODE = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    423: "ACCOUNT_LOCKED",
    429: "TOO_MANY_REQUESTS",
}


@app.exception_handler(HTTPException)
async def httpExceptionHandler(request: Request, exc: HTTPException) -> JSONResponse:
    code = getattr(exc, "errorCode", None) or _STATUS_TO_CODE.get(exc.status_code, "ERROR")
    error = {"code": code, "message": exc.detail}
    # Only exceptions that set `errorDetails` (e.g. FieldsUndecidedError) add "details".
    details = getattr(exc, "errorDetails", None)
    if details:
        error["details"] = details
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=getattr(exc, "headers", None),  # e.g. Retry-After on a 429
    )


@app.exception_handler(RequestValidationError)
async def validationExceptionHandler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = {str(e["loc"][-1]): e["msg"] for e in exc.errors()}
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": errors,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandledExceptionHandler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so an unexpected error (e.g. a DB race not covered
    by an explicit HTTPException) still returns the standard envelope instead
    of Starlette's default plain-text 500.
    """
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}},
    )


# ── Web developer routers ─────────────────────────────────────────────────────

app.include_router(lab_requests_router)
app.include_router(labeling_router)
app.include_router(patients.router)
app.include_router(queue.router)
app.include_router(patient_portal.router)
app.include_router(result_releasing_router)
app.include_router(auth.router)
app.include_router(patient_auth.router)
app.include_router(physician.router)
app.include_router(src_specimens_router)

# ── Mobile developer routers ──────────────────────────────────────────────────

app.include_router(sync_router)
app.include_router(results_router)
app.include_router(images_router)
app.include_router(fields_router)
app.include_router(notifications_router)


@app.get("/", response_model=HealthCheckResponse, tags=["health"])
def rootHealthCheck() -> HealthCheckResponse:
    """Liveness check — confirms the process is up, not that its dependencies are."""
    return HealthCheckResponse(status="healthy", service="UroLens Core Platform Architecture")


_DB_PROBE_TIMEOUT_SECONDS = 2.0


async def _dbReachable() -> bool:
    """`SELECT 1` on a pooled connection, giving up after 2 seconds."""
    try:
        async with AsyncSessionLocal() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), _DB_PROBE_TIMEOUT_SECONDS)
        return True
    except Exception:
        logger.warning("Readiness: database check failed", exc_info=True)
        return False


@app.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Database or model not ready."}},
    tags=["health"],
)
async def readinessCheck() -> JSONResponse:
    """Readiness check — 200 only when the database answers and the AI model is loaded.

    The body carries no detail beyond the two booleans.
    """
    db = await _dbReachable()
    model = engine_preload.isModelReady()
    return JSONResponse(
        status_code=200 if db and model else 503,
        content={"db": db, "model": model},
    )
