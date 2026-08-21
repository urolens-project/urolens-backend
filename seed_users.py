import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from src.urolens.core.supabase import supabase
from src.urolens.core.auth_service import hash_password

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


async def seed_patients_and_results():
    from src.urolens.core.encryption import encrypt_pii

    # ── Look up existing seeded data ─────────────────────────────────────────
    patient_user = await supabase.table("users").select("*").eq("username", "patient").maybe_single().execute()
    if not patient_user.data:
        print("ERROR: 'patient' user not found. Run seed_users() first.")
        return
    patient_user_id = patient_user.data["user_id"]

    medtech_user = await supabase.table("users").select("*").eq("username", "medtech").maybe_single().execute()
    medtech_id = medtech_user.data["user_id"] if medtech_user.data else None

    existing_patient = await supabase.table("patients").select("*").eq("user_id", patient_user_id).maybe_single().execute()

    if existing_patient.data:
        print("  Skipping patient creation for 'patient' user (already exists)")
        patient_id = existing_patient.data["patient_id"]
    else:
        patient_result = await supabase.table("patients").insert({
            "patient_uid": "PAT-100001",
            "first_name": encrypt_pii("Maria"),
            "last_name": encrypt_pii("Santos"),
            "date_of_birth": encrypt_pii("1990-06-15"),
            "sex": "FEMALE",
            "contact_no": encrypt_pii("+639171112222"),
            "address": encrypt_pii("123 Rizal Ave, Manila"),
            "is_walkin": False,
            "record_flag": "COMPLETE",
            "created_by": patient_user_id,
            "user_id": patient_user_id,
            "created_at": "2026-05-01T08:00:00Z",
            "updated_at": "2026-05-01T08:00:00Z",
        }).execute()
        patient_id = patient_result.data[0]["patient_id"]
        print(f"  Created patient record for 'patient' user: {patient_id}")

    # ── Look up existing specimens ──────────────────────────────────────────
    existing_specimens = await supabase.table("specimens").select("specimen_id").execute()
    specimen_ids = [r["specimen_id"] for r in (existing_specimens.data or [])]

    # ── Create sample analysis results ──────────────────────────────────────
    existing_results = await supabase.table("analysis_results").select("result_id").eq("patient_id", patient_id).execute()
    if existing_results.data:
        print(f"  Skipping results for patient {patient_id} (already seeded)")
        return

    created_count = 0

    sample_results = [
        {
            "status": "RELEASED",
            "cell_counts": {"rbc": 2, "wbc": 5, "epithelial_cells": 1, "casts": 0, "bacteria": 0, "crystals": 0, "mucus_threads": 0},
            "interpretation": "Normal result. No significant abnormalities detected. RBC and WBC counts within normal range.",
            "medtech_name": "Juan dela Cruz, RMT",
            "pathologist_name": "Dr. Maria Clara Reyes, MD",
            "pathologist_license": "PRC-0012345",
            "released_at": "2026-05-15T10:00:00Z",
            "confirmed_at": "2026-05-14T14:30:00Z",
            "created_at": "2026-05-14T08:00:00Z",
            "updated_at": "2026-05-15T10:00:00Z",
        },
        {
            "status": "RELEASED",
            "cell_counts": {"rbc": 12, "wbc": 20, "epithelial_cells": 3, "casts": 2, "bacteria": 1, "crystals": 1, "mucus_threads": 1},
            "interpretation": "Elevated WBC count suggests possible urinary tract infection. Presence of casts may indicate renal involvement. Correlate clinically.",
            "medtech_name": "Juan dela Cruz, RMT",
            "pathologist_name": "Dr. Maria Clara Reyes, MD",
            "pathologist_license": "PRC-0012345",
            "released_at": "2026-05-20T09:00:00Z",
            "confirmed_at": "2026-05-19T16:00:00Z",
            "created_at": "2026-05-19T08:30:00Z",
            "updated_at": "2026-05-20T09:00:00Z",
        },
        {
            "status": "PENDING",
            "cell_counts": {"rbc": 1, "wbc": 3, "epithelial_cells": 2, "casts": 0, "bacteria": 0, "crystals": 0, "mucus_threads": 0},
            "interpretation": None,
            "medtech_name": None,
            "pathologist_name": None,
            "pathologist_license": None,
            "released_at": None,
            "confirmed_at": None,
            "created_at": "2026-05-25T09:00:00Z",
            "updated_at": "2026-05-25T09:00:00Z",
        },
    ]

    for result_data in sample_results:
        try:
            spec_id = specimen_ids[created_count % len(specimen_ids)] if specimen_ids else None
            if spec_id is None:
                print("  WARNING: No specimens found. Using placeholder specimen_id.")
                continue

            payload = {
                "specimen_id": spec_id,
                "patient_id": patient_id,
                "status": result_data["status"],
                "cell_counts": result_data["cell_counts"],
                "interpretation": result_data.get("interpretation"),
                "medtech_name": result_data.get("medtech_name"),
                "pathologist_name": result_data.get("pathologist_name"),
                "pathologist_license": result_data.get("pathologist_license"),
                "released_at": result_data.get("released_at"),
                "confirmed_at": result_data.get("confirmed_at"),
                "created_at": result_data["created_at"],
                "updated_at": result_data["updated_at"],
                "ai_findings": {},
                "flagged_anomalies": {},
                "particle_classes": {},
                "model_version": "mvp-v1.0",
                "smart_diagnosis_unavailable": False,
            }
            if medtech_id:
                payload["medtech_id"] = medtech_id
                payload["confirmed_by"] = medtech_id

            await supabase.table("analysis_results").insert(payload).execute()
            print(f"  Created analysis_result (status={result_data['status']})")
            created_count += 1
        except Exception as e:
            print(f"  WARNING: Failed to create result: {e}")

    print(f"\nSeeded {created_count} sample analysis result(s) for patient {patient_id}")


if __name__ == "__main__":
    asyncio.run(seed())
    asyncio.run(seed_patients_and_results())
