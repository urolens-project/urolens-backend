import os

SUPABASE_URL = os.getenv(
    "SUPABASE_URL",
    "https://fctpcvqkqokzizjcirpl.supabase.co",
)
SUPABASE_SERVICE_KEY = os.getenv(
    "SUPABASE_SERVICE_KEY",
    "sb_secret_X8GWf-389TnwObK4fhQflQ_KNHJ5CF3",
)
JWT_SIGNING_KEY = os.getenv(
    "JWT_SIGNING_KEY",
    "change-me-in-production-use-a-long-random-string",
)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8
MAX_FAILED_ATTEMPTS = 5
