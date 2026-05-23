import os

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/urolens_db",
)
JWT_SIGNING_KEY = os.getenv(
    "JWT_SIGNING_KEY",
    "change-me-in-production-use-a-long-random-string",
)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
MAX_FAILED_ATTEMPTS = 5
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
