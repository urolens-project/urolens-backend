from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.results import router as results_router
from app.api.specimens import router as specimens_router
from app.api.sync import router as sync_router
from src.urolens.domains.intake.router import router as intake_router
from src.urolens.domains.intake.specimens_router import router as specimens_router
from src.urolens.domains.request.lab_requests_router import router as lab_requests_router

app = FastAPI(title="UroLens LIS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = getattr(exc, "error_code", None) or _STATUS_TO_CODE.get(exc.status_code, "ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": exc.detail}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
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


# ── Web developer routers ─────────────────────────────────────────────────────
app.include_router(intake_router)
app.include_router(specimens_router)
app.include_router(lab_requests_router)

# ── Mobile developer routers ──────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(sync_router)
app.include_router(specimens_router)
app.include_router(results_router)


@app.get("/")
def root_health_check():
    return {"status": "healthy", "service": "UroLens Core Platform Architecture"}
