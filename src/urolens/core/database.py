import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
# Reads your service key from the .env
supabase_key = os.getenv("SUPABASE_SERVICE_KEY") 

if not supabase_url or not supabase_key:
    raise ValueError("❌ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env file!")

# This client replaces your old SessionLocal engines
supabase: Client = create_client(supabase_url, supabase_key)

# Keep a dummy function here just in case other files import it to prevent crashes
def get_db():
    yield supabase