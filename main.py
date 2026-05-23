from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.urolens.core.database import get_db

from src.urolens.domains.intake.router import router as intake_router
from src.urolens.domains.intake.specimens_router import router as specimens_router
from src.urolens.domains.request.lab_requests_router import router as lab_requests_router

# Initialize tables from metadata bound to engine
# Base.metadata.create_all(bind=engine)

app = FastAPI(title="UroLens LIS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register feature domains
app.include_router(intake_router)
app.include_router(specimens_router)
app.include_router(lab_requests_router)

@app.get("/")
def root_health_check():
    return {"status": "healthy", "service": "UroLens Core Platform Architecture"}