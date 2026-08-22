# urolens-backend

Core LIS Transactions & Business Logic Engine (FastAPI + PostgreSQL + Alembic DB Migrations) with RA 10173 encryption.

## Backend Setup

### Prerequisites

- Python 3.13
- A PostgreSQL database (local instance or a Supabase project)

### 1. Clone and create a virtual environment

```bash
git clone <this-repo>
cd urolens-backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

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

`src/urolens/core/config.py` validates `JWT_SIGNING_KEY` and `ENCRYPTION_KEY` **at import time** — the app will refuse to start with a `RuntimeError` if either is unset or still the placeholder value from `.env.example`. If `python -c "import main"` fails immediately, check `.env` first.

### 4. Run database migrations

```bash
alembic upgrade head
```

Sanity check after pulling new migrations or before opening a PR that adds one: `alembic heads` should print exactly one revision. More than one means a branch needs merging (see `CONTRIBUTING.md`).

### 5. Run the dev server

```bash
uvicorn main:app --reload
```

- Health check: `GET http://127.0.0.1:8000/`
- Interactive API docs: `http://127.0.0.1:8000/docs`

### 6. (Optional) Seed test data

Run in this exact order — `seed_results.py` depends on data the first two create:

```bash
python seed_users.py       # test staff accounts (e.g. a MEDTECH login)
python seed_specimens.py   # test specimen records
python seed_results.py     # analysis results + Smart Diagnosis output + manual overrides
```

### 7. Run the test suite

```bash
pytest
```

The suite currently holds at a known baseline of pre-existing failures (integration tests that need real infra, and a couple of sync-tests-calling-async-functions bugs) alongside a larger and growing set of passing unit tests — a failure count that doesn't match what you started with means something changed, not that the whole suite is expected to be green. When working on a specific area, scope the run instead of reading the whole-suite count, e.g.:

```bash
pytest tests/test_lab_request_service.py
```

### 8. Run the linter

[Ruff](https://docs.astral.sh/ruff/) is configured in `pyproject.toml` (categories `E`, `F`, `I`, `D`, `T20`, `ANN`, `B`, `UP`, `S`) — reporting only today, nothing build-blocking yet:

```bash
ruff check .              # report violations
ruff check --fix .        # apply Ruff's safe autofixes only
ruff format --diff .      # preview what the formatter would change (not applied/enforced yet)
```

See `docs/ruff-baseline-report.md` for the current violation baseline.

## Next steps

- **Adding a feature?** See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the required sequence and pre-PR checklist.
- **Coding standards and security rules:** see [`docs/backend-standards.md`](./docs/backend-standards.md).
