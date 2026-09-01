# 🤝 Contributing to urolens-backend

This document covers the required sequence for adding a new feature and the checklist to run
through before opening a PR. For the full rule-by-rule reference — why each rule exists, and
its current real-world status in this codebase — see
[`docs/backend-standards.md`](./docs/backend-standards.md) 📋. Coding agents working in this
repo should load the `urolens-backend-standards` skill, which mirrors that document.

---

## 🚫 Coding agents: git restrictions

**Coding agents must never run any git command that changes repository or branch state** —
no `commit`, `push`, `pull`, `merge`, `rebase`, `reset`, or branch create/delete. This applies
regardless of what a specific task or prompt asks for.

An agent's job ends at leaving changes as local, uncommitted file edits. A human reviews the
diff and handles every commit, branch, and push manually.

This is enforced two ways, not just requested:

- ⚙️ **`.claude/settings.json`** denies the relevant `Bash` invocations at the
  tool-permission level (`git commit`, `git push`, `git branch -D`, `git merge`, etc.) —
  checked into source control, applies to everyone using Claude Code against this repo.
- 🪝 **A `commit-msg` pre-commit hook** rejects any commit containing an AI co-author trailer
  (`Co-Authored-By: Claude`, etc.) as a backstop, in case an agent's own attribution wasn't
  disabled and a commit slipped through anyway.

> ⚠️ If you're prompting an agent for a task in this repo, don't rely on the tooling alone —
> state the restriction explicitly in the prompt too.

---

## 🧩 Adding a new feature — required sequence

1. 🗂️ **Check `src/schemas/`** for an existing request/response shape before defining
   a new one. Several domain modules already export a wide surface via `schemas/__init__.py`.
2. 🧠 **Check `src/services/`** for existing logic before writing new service code —
   the service layer is organized one file per domain (`patient_service.py`,
   `queue_service.py`, etc.), and `services/__init__.py` re-exports the public
   classes/functions.
3. ⚙️ **Add or extend a service** in `src/services/<feature>_service.py`.
4. 🔐 **Add or extend a router** in `src/api/`. Every protected route must use the canonical dependency from
   `src/core/rbac.py` — `Depends(RequireRole([...]))` or `Depends(getCurrentUser)` —
   never a bespoke auth check.
5. 📌 **Register the router in `main.py`.** Check the existing `app.include_router(...)`
   calls first to confirm no other router already serves the path you're adding.
6. 📦 **Prefer importing from a package's barrel where practical.** `core/`, `services/`,
   `schemas/`, and `models/` all have populated `__init__.py` files re-exporting their public
   API (e.g. `from src.services import PatientService`). This is additive — most
   *existing* call sites still import submodules directly
   (`from src.services.patient_service import PatientService`), and that hasn't been
   migrated yet — but new code should reach for the barrel first. `api/` doesn't
   have a barrel: every router module exports a symbol literally named `router`, which would
   collide across all of them. Import those submodules directly.
7. 📖 **Verify the OpenAPI docs render correctly.** Start the server
   (`uvicorn main:app --reload`) and check `/docs` for the new route before considering the
   change done.
8. 🌱 **New environment variable?** Add it to `.env`, `.env.example`, and
   `src/core/config.py`'s `Settings` class — with startup validation if it's a
   secret, following the pattern already used for `JWT_SIGNING_KEY`/`ENCRYPTION_KEY`.
9. 🗃️ **Touching the database?**
   - Run `alembic heads` first — must show a single head before you start.
   - Write one migration per change.
   - Use `psycopg2` idioms for the migration itself (Alembic's runtime); write
     `asyncpg`-compatible code for anything the app reads/writes at request time.
   - Never hand-apply SQL out of band. If a table or column needs to exist, it needs a
     migration — even if you could add it manually in Supabase's SQL editor instead.
10. 🧪 **Tag a risk tier and add matching tests.** See `docs/backend-standards.md`'s testing
    rule for the three tiers. Follow the existing convention in `tests/`: one test file per
    service (`tests/test_<service>.py`), `AsyncMock`/`MagicMock(spec=Model)` fixtures, one
    happy-path test per behavior plus the guard conditions.

---

## ✅ Pre-PR checklist

- [ ] 🔐 Every new/changed route uses the canonical auth/RBAC dependency
      (`RequireRole`/`getCurrentUser` from `core.rbac`).
- [ ] 📝 Every new/changed exported function/class/constant has a docstring — one-line
      summary, `Args:`/`Returns:`/`Raises:` sections where applicable (Google style — see
      `pyproject.toml`'s `pydocstyle.convention`).
- [ ] 🏥 No `print(...)` in application code — use logging. No hardcoded actor UUID. No
      client-supplied identity header trusted as auth.
- [ ] 🗂️ No inline `BaseModel`/dict shape defined in a router or service — it belongs in
      `schemas/`.
- [ ] 📌 No new floating/unpinned dependency in `requirements.txt` — pin to an exact version
      (or a specific tag/commit for a git dependency, never a mutable branch).
- [ ] 🗃️ `alembic heads` shows a single head.
- [ ] 🐫 Naming follows `docs/backend-standards.md`'s rule 17 — run
      `python scripts/check_naming.py`.
- [ ] ♻️ A superseded implementation is deleted in the same change, not left dead alongside
      its replacement.
- [ ] 🧪 Tests match the feature's risk tier. Run `pytest` on the area you touched and
      confirm you haven't shifted the pre-existing pass/fail baseline elsewhere.
- [ ] 🧹 `ruff check .` shows no new violations in a category you're touching — nothing is
      build-blocking yet, but don't add to the pile knowingly. `ruff check --fix .` is safe
      to run on files you've changed.
