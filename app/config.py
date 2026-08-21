import os
from dotenv import load_dotenv

load_dotenv(override=True)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/urolens_db",
)
JWT_SIGNING_KEY = os.getenv("JWT_SIGNING_KEY")
if not JWT_SIGNING_KEY or JWT_SIGNING_KEY == "change-me-in-production-use-a-long-random-string":
    raise RuntimeError(
        "JWT_SIGNING_KEY is unset or using the placeholder default. "
        "Set a strong, random JWT_SIGNING_KEY in the environment before starting the app."
    )
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
MAX_FAILED_ATTEMPTS = 5
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY is unset. Set a Fernet key (Fernet.generate_key()) in the "
        "environment before starting the app — PHI encryption cannot run without it."
    )
SUPABASE_IMAGE_BUCKET = os.getenv("SUPABASE_IMAGE_BUCKET", "microscopy")
