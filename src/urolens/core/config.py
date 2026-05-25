from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/urolens_db",
)

# Async URL — swap driver for asyncpg if a sync URL was provided
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

JWT_SIGNING_KEY: str = os.getenv(
    "JWT_SIGNING_KEY",
    "change-me-in-production-use-a-long-random-string",
)
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "8"))

S3_BUCKET: str = os.getenv("S3_BUCKET", "urolens-images")
S3_REGION: str = os.getenv("S3_REGION", "ap-southeast-1")

AI_MODEL_VERSION: str = os.getenv("AI_MODEL_VERSION", "mvp-v1.0")
