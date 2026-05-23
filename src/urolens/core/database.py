import os
from dotenv import load_dotenv
from supabase import create_client, Client
from sqlalchemy.orm import declarative_base

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("❌ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env file!")

supabase: Client = create_client(supabase_url, supabase_key)

Base = declarative_base()


def get_db():
    yield supabase