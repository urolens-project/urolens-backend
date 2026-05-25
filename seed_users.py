import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from app.db.supabase import supabase
from app.services.auth_service import hash_password

SEED_USERS = [
    {
        "username": "medtech",
        "password": "password123",
        "role": "MEDTECH",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "receptionist",
        "password": "password123",
        "role": "RECEPTIONIST",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "supervisor",
        "password": "password123",
        "role": "SUPERVISOR",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "physician",
        "password": "password123",
        "role": "PHYSICIAN",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "patient",
        "password": "password123",
        "role": "PATIENT",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "administrator",
        "password": "password123",
        "role": "ADMINISTRATOR",
        "is_active": True,
        "locked_at": None,
        "failed_attempts": 0,
    },
    {
        "username": "locked_receptionist",
        "password": "password123",
        "role": "RECEPTIONIST",
        "is_active": True,
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "failed_attempts": 5,
    },
]


async def seed():
    for entry in SEED_USERS:
        existing = await supabase.table("users").select("*").eq(
            "username", entry["username"]
        ).execute()

        if existing.data:
            print(f"  Skipping {entry['username']} (already exists)")
            continue

        await supabase.table("users").insert(
            {
                "username": entry["username"],
                "hashed_password": hash_password(entry["password"]),
                "role": entry["role"],
                "is_active": entry["is_active"],
                "locked_at": entry.get("locked_at"),
                "failed_attempts": entry.get("failed_attempts", 0),
            }
        ).execute()
        print(f"  Created {entry['username']}")

    print("\nSeed complete.\n")
    print("Test credentials (all passwords: password123):")
    for entry in SEED_USERS:
        locked_note = " [LOCKED]" if entry.get("locked_at") else ""
        print(f"  {entry['username']:<24} role={entry['role']}{locked_note}")


if __name__ == "__main__":
    asyncio.run(seed())
