# Contributing to urolens-backend

This document covers the required sequence for adding a new feature and the checklist to run through before opening a PR. For the full rule-by-rule reference (why each rule exists, and its current real-world status in this codebase), see [`docs/backend-standards.md`](./docs/backend-standards.md).

## Adding a new feature — required sequence

1. **Check `src/urolens/schemas/`** for an existing request/response shape before defining a new one. Several domain modules already export a wide surface via `schemas/__init__.py`.
2. **Check `src/urolens/services/`** for existing logic before writing new service code — the service layer is organized one file per domain (`patient_service.py`, `queue_service.py`, etc.), and `services/__init__.py` re-exports the public classes/functions.
3. **Add or extend a service** in `src/urolens/services/<feature>_service.py`.
4. **Add or extend a router** in `src/urolens/api/` (or `src/urolens/domains/<feature>/` for the two domain-specific router groups — specimen intake and lab-request creation currently live there). Every protected route must use the canonical dependency from `src/urolens/core/rbac.py` — `Depends(RequireRole([...]))` or `Depends(get_current_user)` — never a bespoke auth check.
5. **Register the router in `main.py`** — check the existing `app.include_router(...)` calls first to make sure no other router already serves the path you're adding.
6. **Prefer importing from a package's barrel where practical.** `core/`, `services/`, `schemas/`, and `models/` all have populated `__init__.py` files re-exporting their public API (e.g. `from src.urolens.services import PatientService`). This is additive — most *existing* call sites in this codebase still import submodules directly (`from src.urolens.services.patient_service import PatientService`), and that hasn't been migrated yet — but new code should reach for the barrel first. `api/` and `domains/` don't have barrels (every router module exports a symbol literally named `router`, which collides across all of them) — import those submodules directly.
7. **Verify the OpenAPI docs render correctly.** Start the server (`uvicorn main:app --reload`) and check `/docs` for the new route before considering the change done.
8. **New environment variable?** Add it to `.env`, `.env.example`, and `src/urolens/core/config.py`'s `Settings` class (with validation at import time if it's a secret — follow the pattern already used for `JWT_SIGNING_KEY`/`ENCRYPTION_KEY`).
9. **Touching the database?** Run `alembic heads` first (must show a single head before you start). Write one migration per change. Use `psycopg2` idioms for the migration itself (Alembic's runtime), `asyncpg`-compatible code for anything the app reads/writes at request time. Never hand-apply SQL out of band — if a table or column needs to exist, it needs a migration, even if you can get away with adding it manually in Supabase's SQL editor.
10. **Tag a risk tier and add matching tests.** See `docs/backend-standards.md`'s testing rule for the three tiers. Look at `tests/` for the existing convention — one test file per service (`tests/test_<service>.py`), `AsyncMock`/`MagicMock(spec=Model)` fixtures, one happy-path test per behavior plus the guard conditions.

## Pre-PR checklist

- [ ] Every new/changed route has the canonical auth/RBAC dependency (`RequireRole`/`get_current_user` from `core.rbac`).
- [ ] Every new/changed exported function/class/constant has a docstring — one-line summary, `Args:`/`Returns:`/`Raises:` sections where applicable (Google style — see `pyproject.toml`'s `pydocstyle.convention`).
- [ ] No `print(...)` in application code (use logging) — no hardcoded actor UUID, no client-supplied identity header trusted as auth.
- [ ] No inline `BaseModel`/dict shape defined in a router or service — it belongs in `schemas/`.
- [ ] No new floating/unpinned dependency in `requirements.txt` — pin to an exact version (or a specific tag/commit for a git dependency, never a mutable branch).
- [ ] `alembic heads` — single head.
- [ ] A superseded implementation is deleted in the same PR, not left dead alongside the replacement.
- [ ] Tests match the feature's risk tier; run `pytest` on the area you touched and confirm you haven't changed the pre-existing pass/fail baseline elsewhere.
- [ ] `ruff check .` — no new violations introduced by your change in a category you're touching (nothing is build-blocking yet, but don't add to the pile knowingly). `ruff check --fix .` is safe to run on files you've changed.
