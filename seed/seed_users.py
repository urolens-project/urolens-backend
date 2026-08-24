import asyncio
from datetime import UTC, datetime

from dotenv import load_dotenv

load_dotenv()

from src.core.auth_service import hashPassword
from src.core.supabase import supabase

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
        "locked_at": datetime.now(UTC).isoformat(),
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
                "hashed_password": hashPassword(entry["password"]),
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
        lockedNote = " [LOCKED]" if entry.get("locked_at") else ""
        print(f"  {entry['username']:<24} role={entry['role']}{lockedNote}")


async def seedPatientsAndResults():
    from src.core.encryption import encryptPii

    # ── Look up existing seeded data ─────────────────────────────────────────
    patientUser = await supabase.table("users").select("*").eq("username", "patient").maybe_single().execute()
    if not patientUser.data:
        print("ERROR: 'patient' user not found. Run seed_users() first.")
        return
    patientUserId = patientUser.data["user_id"]

    medtechUser = await supabase.table("users").select("*").eq("username", "medtech").maybe_single().execute()
    medtechId = medtechUser.data["user_id"] if medtechUser.data else None

    existingPatient = await supabase.table("patients").select("*").eq("user_id", patientUserId).maybe_single().execute()

    if existingPatient.data:
        print("  Skipping patient creation for 'patient' user (already exists)")
        patientId = existingPatient.data["patient_id"]
    else:
        patientResult = await supabase.table("patients").insert({
            "patient_uid": "PAT-100001",
            "first_name": encryptPii("Maria"),
            "last_name": encryptPii("Santos"),
            "date_of_birth": encryptPii("1990-06-15"),
            "sex": "FEMALE",
            "contact_no": encryptPii("+639171112222"),
            "address": encryptPii("123 Rizal Ave, Manila"),
            "is_walkin": False,
            "record_flag": "COMPLETE",
            "created_by": patientUserId,
            "user_id": patientUserId,
            "created_at": "2026-05-01T08:00:00Z",
            "updated_at": "2026-05-01T08:00:00Z",
        }).execute()
        patientId = patientResult.data[0]["patient_id"]
        print(f"  Created patient record for 'patient' user: {patientId}")

    # ── Look up existing specimens ──────────────────────────────────────────
    existingSpecimens = await supabase.table("specimens").select("specimen_id").execute()
    specimenIds = [r["specimen_id"] for r in (existingSpecimens.data or [])]

    # ── Create sample analysis results ──────────────────────────────────────
    existingResults = await supabase.table("analysis_results").select("result_id").eq("patient_id", patientId).execute()
    if existingResults.data:
        print(f"  Skipping results for patient {patientId} (already seeded)")
        return

    createdCount = 0

    sampleResults = [
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

    for resultData in sampleResults:
        try:
            specId = specimenIds[createdCount % len(specimenIds)] if specimenIds else None
            if specId is None:
                print("  WARNING: No specimens found. Using placeholder specimen_id.")
                continue

            payload = {
                "specimen_id": specId,
                "patient_id": patientId,
                "status": resultData["status"],
                "cell_counts": resultData["cell_counts"],
                "interpretation": resultData.get("interpretation"),
                "medtech_name": resultData.get("medtech_name"),
                "pathologist_name": resultData.get("pathologist_name"),
                "pathologist_license": resultData.get("pathologist_license"),
                "released_at": resultData.get("released_at"),
                "confirmed_at": resultData.get("confirmed_at"),
                "created_at": resultData["created_at"],
                "updated_at": resultData["updated_at"],
                "ai_findings": {},
                "flagged_anomalies": {},
                "particle_classes": {},
                "model_version": "mvp-v1.0",
                "smart_diagnosis_unavailable": False,
            }
            if medtechId:
                payload["medtech_id"] = medtechId
                payload["confirmed_by"] = medtechId

            await supabase.table("analysis_results").insert(payload).execute()
            print(f"  Created analysis_result (status={resultData['status']})")
            createdCount += 1
        except Exception as e:
            print(f"  WARNING: Failed to create result: {e}")

    print(f"\nSeeded {createdCount} sample analysis result(s) for patient {patientId}")


if __name__ == "__main__":
    asyncio.run(seed())
    asyncio.run(seedPatientsAndResults())
