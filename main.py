from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.urolens.core.database import get_db

from src.urolens.domains.intake.router import router as intake_router
from src.urolens.domains.request.lab_requests_router import router as lab_requests_router
from src.urolens.api import patients
from app.api import auth

app = FastAPI(title="UroLens LIS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(intake_router)
app.include_router(lab_requests_router)
app.include_router(patients.router)
app.include_router(auth.router)

@app.get("/")
def root_health_check():
    return {"status": "healthy", "service": "UroLens Core Platform Architecture"}