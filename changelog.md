# Changelog

## Unreleased

### Fixed
- **CORS misconfiguration**: `main.py` combined `allow_origins=["*"]` with
  `allow_credentials=True`, which lets any origin read authenticated responses made
  with the browser's ambient credentials. The API only authenticates via a Bearer
  token in the `Authorization` header (never cookies — confirmed no
  `withCredentials`/`credentials: include` anywhere in `urolens-web`), so credentialed
  CORS was never needed. Set `allow_credentials=False`.
- **Hardcoded JWT signing-key fallback**: both `app/config.py` and
  `src/urolens/core/config.py` defaulted `JWT_SIGNING_KEY` to the literal string
  `"change-me-in-production-use-a-long-random-string"` if the env var was unset, so a
  misconfigured deployment would silently sign tokens with a public, guessable key.
  Both modules now raise `RuntimeError` at import time if `JWT_SIGNING_KEY` is unset
  or still equal to that placeholder.

These two fixes were pulled out of the in-progress backend consolidation
(`docs/backend-consolidation-plan.md`) and shipped independently since they're
single-file, don't depend on any of the consolidation's open decisions (which
service layer survives, which RBAC import wins), and were live security gaps.

## Track A2 — minimal PHI auth patch (rows 9–11 of the consolidation plan)

### Fixed
- **Auth added to three previously-unauthenticated routers**: `receive_specimen_endpoint`,
  `list_specimens_endpoint`, and `search_pending_lab_requests` in
  `src/urolens/domains/intake/specimens_router.py`; `generate_specimen_label_endpoint`,
  `confirm_label_affixed_endpoint`, and `search_received_specimens` in
  `src/urolens/domains/intake/labeling_router.py`; and `create_lab_request_endpoint` and
  `get_physicians_endpoint` in `src/urolens/domains/request/lab_requests_router.py`. All
  eight now require the canonical session-revocation-aware dependency
  (`app.middleware.rbac.RequireRole` paired with `src.urolens.core.enums.UserRole`) —
  the same pair already used correctly by `patients.py`/`queue.py`/`result_releasing.py`.
  Route logic, request/response shapes, and business behavior are otherwise unchanged;
  the full encryption/service-layer/ID-generation rework for these three files stays
  scoped to the later domain merge (plan doc rows 9–11).
- **Removed the `X-User-Id`-header-as-identity pattern**: `labeling_router.py`'s
  `get_current_user_id` read a client-supplied `X-User-Id` header as the caller's
  identity, falling back to a hardcoded UUID if absent — a forbidden pattern per the
  standards skill. Deleted outright; its one call site (`generate_specimen_label_endpoint`)
  now resolves the operator from the real authenticated user instead.
- **Actor attribution now uses the real authenticated user**: the hardcoded
  `CURRENT_RECEPTIONIST_ID`/`CURRENT_ENCODER_ID` constants and the hardcoded
  `generated_by` UUID in `labeling_router.py`'s offline-label path are gone — each
  patched route now attributes its writes to the user resolved by the new auth
  dependency. This falls directly out of adding real auth (a route that can identify
  its caller has no remaining reason to hardcode one) and wasn't a separate task.
- **Removed the unused `PHYSICIAN_UUID_MAP`** from `lab_requests_router.py` — a dead,
  unreferenced dict hardcoding three UUIDs (one not even valid hex) with a comment
  telling a future dev to replace them.
- **`ENCRYPTION_KEY` now validated at startup**: `app/config.py` previously defaulted
  it to `""`, which only failed the first time `encrypt_pii`/`decrypt_pii` actually ran.
  Now raises `RuntimeError` at import time if unset, same pattern as the JWT fix.
  `src/urolens/core/config.py` was left alone — confirmed (via
  `src/urolens/core/encryption.py`) that encryption always reads `ENCRYPTION_KEY` from
  `app.config`, never from `src/urolens/core/config.py`, so there's nothing to add there.
- **Alembic merge revision `0031`** joins the two previously-orphaned heads (`0011`,
  the notifications branch, and `0030`, the main branch) — pure metadata, no schema
  change. `alembic heads` now reports a single head.
- **Two migration filename/revision-ID mismatches fixed**: renamed
  `0015_analysis_results_add_patient_portal_fields.py` → `0027_...py` and
  `0016_patients_add_user_id.py` → `0028_...py` to match their embedded `revision`
  values (revision content itself untouched).
- **Doc cross-reference corrected**: `docs/backend-consolidation-plan.md` row 3
  pointed to "#7 below" for an urgency comparison that row 7 doesn't support (it's a
  sizing note, not an urgency ranking); repointed to rows 9–10, the actual other
  live-PHI/unauthenticated findings.
