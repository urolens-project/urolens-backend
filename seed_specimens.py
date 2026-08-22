import asyncio
from datetime import UTC, datetime, timedelta

from src.urolens.core.supabase import supabase

# Adjust status values to match the specimens.status enum in your DB.
# Run this in Supabase SQL Editor to see valid values:
#   SELECT enumlabel FROM pg_enum
#   JOIN pg_type ON pg_enum.enumtypid = pg_type.oid
#   WHERE pg_type.typname LIKE '%specimen%' OR pg_type.typname LIKE '%status%';
SPECIMENS = [
    {
        "sample_uid":           "SAMPLE-88241",
        "patient_name":         "Sarah Jenkins",
        "patient_uid":          "PT-001",
        "test_type":            "Urinalysis",
        "status":               "ASSIGNED",
        "priority_level":       "HIGH",
        "hours_ago_received":   2,
        "hours_ago_assigned":   1,
    },
    {
        "sample_uid":           "SAMPLE-88242",
        "patient_name":         "John Doe",
        "patient_uid":          "PT-002",
        "test_type":            "Urinalysis",
        "status":               "ASSIGNED",
        "priority_level":       "NORMAL",
        "hours_ago_received":   3,
        "hours_ago_assigned":   2,
    },
    {
        "sample_uid":           "SAMPLE-88239",
        "patient_name":         "Michael Vance",
        "patient_uid":          "PT-003",
        "test_type":            "Urinalysis",
        "status":               "ASSIGNED",
        "priority_level":       "HIGH",
        "hours_ago_received":   4,
        "hours_ago_assigned":   3,
    },
]


async def seed():
    # Look up the medtech user
    result = await supabase.table("users").select("user_id").eq("username", "medtech").maybe_single().execute()
    if not result.data:
        print("ERROR: 'medtech' user not found. Run seed_users.py first.")
        return

    medtech_id = result.data["user_id"]
    print(f"Found medtech user: {medtech_id}")

    # Fetch existing lab_request IDs to satisfy the FK constraint
    lab_reqs = await supabase.table("lab_requests").select("lab_request_id").execute()
    if not lab_reqs.data:
        print("ERROR: No lab_requests found. The web developer must seed lab_requests first.")
        return
    lab_request_ids = [r["lab_request_id"] for r in lab_reqs.data]
    print(f"Found {len(lab_request_ids)} lab_request(s) to use\n")

    now = datetime.now(UTC)

    for i, spec in enumerate(SPECIMENS):
        # Skip if already seeded
        existing = await supabase.table("specimens").select("sample_uid").eq("sample_uid", spec["sample_uid"]).execute()
        if existing.data:
            print(f"  Skipping {spec['sample_uid']} (already exists)")
            continue

        received_at = (now - timedelta(hours=spec["hours_ago_received"])).isoformat()
        assigned_at = (now - timedelta(hours=spec["hours_ago_assigned"])).isoformat()

        # Insert specimen
        spec_result = await supabase.table("specimens").insert({
            "sample_uid":     spec["sample_uid"],
            "patient_name":   spec["patient_name"],
            "patient_uid":    spec["patient_uid"],
            "test_type":      spec["test_type"],
            "status":         spec["status"],
            "priority_level": spec["priority_level"],
            "received_at":    received_at,
            "assigned_at":    assigned_at,
            "medtech_id":     medtech_id,
            "lab_request_id": lab_request_ids[i % len(lab_request_ids)],  # rotate through existing lab_requests
            "received_by":    medtech_id,
        }).execute()

        specimen_id = spec_result.data[0]["specimen_id"]

        # Insert queue assignment (assignment_id is the PK in existing schema)
        await supabase.table("queue_assignments").insert({
            "specimen_id": specimen_id,
            "medtech_id":  medtech_id,
            "assigned_by": medtech_id,
            "assigned_at": assigned_at,
            "status":      "ACTIVE",
        }).execute()

        print(f"  Created {spec['sample_uid']} — {spec['patient_name']} [{spec['priority_level']}]")

    print("\nSpecimen seed complete.")
    print("\nVerify in Supabase SQL Editor:")
    print("  SELECT s.sample_uid, s.patient_name, s.status, q.status AS queue_status")
    print("  FROM specimens s JOIN queue_assignments q ON q.specimen_id = s.specimen_id;")


if __name__ == "__main__":
    asyncio.run(seed())
