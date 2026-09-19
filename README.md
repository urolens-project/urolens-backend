# 🔬 UROLENS-BACKEND

Core LIS Transactions & Business Logic Engine (FastAPI + PostgreSQL + Alembic DB Migrations)
with RA 10173 encryption.

---

## 🛠️ Backend Setup

### 📋 Prerequisites

- Python 3.13
- A PostgreSQL database (local instance or a Supabase project)

### 1️⃣ 📥 Clone and create a virtual environment

```bash
git clone <this-repo>
cd urolens-backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2️⃣ 📦 Install dependencies

```bash
pip install -r requirements.txt
```

Then install the git hooks — both commands are required:

```bash
pre-commit install
pre-commit install --hook-type commit-msg
```

> 🪝 `commit-msg` is a separate hook type from the default `pre-commit` stage and is **not**
> installed by the first command alone — skipping the second means the AI-co-author-trailer
> and conventional-commit checks defined in `.pre-commit-config.yaml` silently never run for
> you.

### 3️⃣ ⚙️ Configure environment variables

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project + service-role key (used by the Supabase-REST-backed services that haven't been ported to SQLAlchemy yet) |
| `DATABASE_URL` | Postgres connection string (psycopg2-style; the app derives the asyncpg-style URL from it automatically) |
| `JWT_SIGNING_KEY` | Signs staff/patient session tokens — **must** be a real random string |
| `ENCRYPTION_KEY` | Fernet key for PHI/PII encryption at rest — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_ALGORITHM`, `JWT_EXPIRY_HOURS`, `MAX_FAILED_ATTEMPTS` | Have working defaults; override only if you need to |

> 🔒 `src/core/config.py` validates `JWT_SIGNING_KEY` and `ENCRYPTION_KEY` **at
> import time** — the app will refuse to start with a `RuntimeError` if either is unset or
> still the placeholder value from `.env.example`. If `python -c "import main"` fails
> immediately, check `.env` first.

### 4️⃣ 🗃️ Run database migrations

```bash
alembic upgrade head
```

Sanity check after pulling new migrations or before opening a PR that adds one:
`alembic heads` should print exactly one revision. More than one means a branch needs
merging (see `docs/CONTRIBUTING.md`).

### 5️⃣ 🚀 Run the dev server

```bash
uvicorn main:app --reload
```

- Health check: `GET http://127.0.0.1:8000/`
- Interactive API docs: `http://127.0.0.1:8000/docs`

### 6️⃣ 🌱 (Optional) Seed test data

Run in this exact order from the repo root — `seed_results.py` depends on data the first two
create:

```bash
python seed/seed_users.py       # test staff accounts (e.g. a MEDTECH login)
python seed/seed_specimens.py   # test specimen records
python seed/seed_results.py     # analysis results + Smart Diagnosis output + manual overrides
```

### 7️⃣ 🧪 Run the test suite

```bash
pytest
```

> 📍 The suite is expected to pass fully — no real infra required, no known pre-existing
> failures. If `pytest` reports any failures on a clean checkout, that's a real regression,
> not a baseline to ignore.

When working on a specific area, scope the run instead of reading the whole-suite count,
e.g.:

```bash
pytest tests/test_lab_request_service.py
```

### 8️⃣ 🧹 Run the linter

[Ruff](https://docs.astral.sh/ruff/) is configured in `pyproject.toml` (categories `E`, `F`,
`I`, `D`, `T20`, `ANN`, `B`, `UP`, `S`) — reporting only today, nothing build-blocking yet:

```bash
ruff check .              # report violations
ruff check --fix .        # apply Ruff's safe autofixes only
ruff format --diff .      # preview what the formatter would change (not applied/enforced yet)
```

See `docs/ruff-baseline-report.md` for the current violation baseline.

---

## 🧭 Next steps

- 🤝 **Adding a feature?** See [`docs/CONTRIBUTING.md`](./docs/CONTRIBUTING.md) for the required
  sequence and pre-PR checklist.
- 📋 **Coding standards and security rules:** see
  [`docs/backend-standards.md`](./docs/backend-standards.md).
