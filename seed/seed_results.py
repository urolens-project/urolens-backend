"""seed_results.py — Creates test data for WEB-09 (Smart Diagnosis) and WEB-10 (Result Review).

What it creates:
  - 3 analysis_results in PENDING_SUPERVISOR_APPROVAL status
  - 3 smart_diagnosis_outputs (one per result, varied scores)
  - 2 manual_overrides (on the first two results)

Pre-requisites:
  Run seed_users.py and seed_specimens.py first.

Usage:
  python seed_results.py
"""

import asyncio
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from src.core.supabase import supabase

_PHT = timezone(timedelta(hours=8))


RESULTS_SEED = [
    {
        "sample_uid": "SAMPLE-88241",
        "ai_findings": {
            "wbc_count": 18,
            "rbc_count": 3,
            "epithelial_cells": "moderate",
            "bacteria": "few",
            "casts": "none",
            "crystals": "uric_acid",
        },
        "flagged_anomalies": {
            "high_wbc": True,
            "uric_acid_crystals": True,
        },
        "particle_classes": {
            "wbc": {"count": 18, "confidence": 0.94},
            "rbc": {"count": 3, "confidence": 0.88},
            "cast_hyaline": {"count": 0, "confidence": 0.0},
            "crystal_uric_acid": {"count": 12, "confidence": 0.91},
        },
        "model_version": "mvp-v1.0",
        "smart_diagnosis": {
            "gout_score": "HIGH",
            "uti_score": "LOW",
            "tricho_score": "LOW",
            "evidence_map": {
                "gout": ["uric_acid_crystals_present", "elevated_wbc"],
                "uti": [],
                "tricho": [],
            },
            "no_significant_indicators": False,
            "engine_version": "sde-v0.3.1",
        },
        "overrides": [
            {
                "parameter_name": "wbc_count",
                "original_ai_value": "18",
                "corrected_value": "22",
                "rationale": "Manual recount under oil immersion confirmed 22 WBCs per HPF.",
            }
        ],
    },
    {
        "sample_uid": "SAMPLE-88242",
        "ai_findings": {
            "wbc_count": 35,
            "rbc_count": 1,
            "epithelial_cells": "many",
            "bacteria": "many",
            "casts": "none",
            "crystals": "none",
        },
        "flagged_anomalies": {
            "high_wbc": True,
            "many_bacteria": True,
        },
        "particle_classes": {
            "wbc": {"count": 35, "confidence": 0.97},
            "rbc": {"count": 1, "confidence": 0.82},
            "bacteria_rod": {"count": 48, "confidence": 0.93},
        },
        "model_version": "mvp-v1.0",
        "smart_diagnosis": {
            "gout_score": "LOW",
            "uti_score": "HIGH",
            "tricho_score": "MODERATE",
            "evidence_map": {
                "gout": [],
                "uti": ["elevated_wbc", "many_bacteria", "many_epithelial_cells"],
                "tricho": ["many_epithelial_cells"],
            },
            "no_significant_indicators": False,
            "engine_version": "sde-v0.3.1",
        },
        "overrides": [
            {
                "parameter_name": "bacteria",
                "original_ai_value": "many",
                "corrected_value": "packed",
                "rationale": "Density exceeded 'many' threshold upon gram stain review.",
            }
        ],
    },
    {
        "sample_uid": "SAMPLE-88239",
        "ai_findings": {
            "wbc_count": 4,
            "rbc_count": 2,
            "epithelial_cells": "few",
            "bacteria": "none",
            "casts": "none",
            "crystals": "none",
        },
        "flagged_anomalies": {},
        "particle_classes": {
            "wbc": {"count": 4, "confidence": 0.91},
            "rbc": {"count": 2, "confidence": 0.86},
        },
        "model_version": "mvp-v1.0",
        "smart_diagnosis": {
            "gout_score": "LOW",
            "uti_score": "LOW",
            "tricho_score": "LOW",
            "evidence_map": {"gout": [], "uti": [], "tricho": []},
            "no_significant_indicators": True,
            "engine_version": "sde-v0.3.1",
        },
        "overrides": [],
    },
]


async def seed():
    # ── Look up medtech user ──────────────────────────────────────────────────
    uRes = await supabase.table("users").select("user_id").eq("username", "medtech").maybe_single().execute()
    if not uRes.data:
        print("ERROR: 'medtech' user not found. Run seed_users.py first.")
        return
    medtechId = uRes.data["user_id"]
    print(f"Found medtech: {medtechId}")

    now = datetime.now(_PHT)

    for i, entry in enumerate(RESULTS_SEED):
        sampleUid = entry["sample_uid"]

        # ── Look up specimen ──────────────────────────────────────────────────
        specRes = await supabase.table("specimens").select("specimen_id").eq("sample_uid", sampleUid).maybe_single().execute()
        if not specRes.data:
            print(f"  SKIP {sampleUid}: specimen not found — run seed_specimens.py first.")
            continue
        specimenId = specRes.data["specimen_id"]

        # ── Skip if analysis_result already exists ────────────────────────────
        existing = await supabase.table("analysis_results").select("result_id").eq("specimen_id", specimenId).maybe_single().execute()
        if existing.data:
            resultId = existing.data["result_id"]
            print(f"  EXISTS {sampleUid} → result_id={resultId[:8]}…")
        else:
            confirmedAt = (now - timedelta(hours=3 - i)).isoformat()
            arRes = await supabase.table("analysis_results").insert({
                "specimen_id": specimenId,
                "ai_findings": entry["ai_findings"],
                "flagged_anomalies": entry["flagged_anomalies"],
                "particle_classes": entry["particle_classes"],
                "model_version": entry["model_version"],
                "status": "PENDING_SUPERVISOR_APPROVAL",
                "smart_diagnosis_unavailable": False,
                "confirmed_by": medtechId,
                "confirmed_at": confirmedAt,
                "confirmation_notes": "Reviewed under microscope. Results verified.",
                "updated_at": confirmedAt,
                "created_at": confirmedAt,
            }).execute()
            resultId = arRes.data[0]["result_id"]
            print(f"  CREATED analysis_result for {sampleUid} → {resultId[:8]}…")

        # ── Smart diagnosis output ────────────────────────────────────────────
        sd = entry["smart_diagnosis"]
        sdExisting = await supabase.table("smart_diagnosis_outputs").select("output_id").eq("result_id", resultId).maybe_single().execute()
        if sdExisting.data:
            print("    smart_diagnosis already exists — skipping")
        else:
            await supabase.table("smart_diagnosis_outputs").insert({
                "result_id": resultId,
                "gout_score": sd["gout_score"],
                "uti_score": sd["uti_score"],
                "tricho_score": sd["tricho_score"],
                "evidence_map": sd["evidence_map"],
                "no_significant_indicators": sd["no_significant_indicators"],
                "engine_version": sd["engine_version"],
                "status": "ATTACHED",
                "generated_at": (now - timedelta(hours=3 - i, minutes=5)).isoformat(),
            }).execute()
            print(f"    smart_diagnosis: gout={sd['gout_score']} uti={sd['uti_score']} tricho={sd['tricho_score']}")

        # ── Manual overrides ──────────────────────────────────────────────────
        for override in entry["overrides"]:
            ovExisting = await supabase.table("manual_overrides").select("override_id").eq("result_id", resultId).eq("parameter_name", override["parameter_name"]).maybe_single().execute()
            if ovExisting.data:
                print(f"    override '{override['parameter_name']}' already exists — skipping")
                continue
            await supabase.table("manual_overrides").insert({
                "result_id": resultId,
                "parameter_name": override["parameter_name"],
                "original_ai_value": override["original_ai_value"],
                "corrected_value": override["corrected_value"],
                "rationale": override["rationale"],
                "medtech_id": medtechId,
                "overridden_at": (now - timedelta(hours=3 - i, minutes=2)).isoformat(),
            }).execute()
            print(f"    override: {override['parameter_name']} {override['original_ai_value']} → {override['corrected_value']}")

    print("\nSeed complete.")
    print("\nTo test WEB-10, log in as supervisor / password123")
    print("and navigate to /supervisor/results")


if __name__ == "__main__":
    asyncio.run(seed())
