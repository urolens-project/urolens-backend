# 🔬 UROLENS-BACKEND

Backend API for **UroLens**, an AI-assisted urinalysis and laboratory information system. It serves the web portal and the mobile app: staff and patient authentication, patient and specimen records, lab requests, multi-field microscope image analysis with the UroLens AI engine, Smart Diagnosis, result review and release, notifications, and an audit trail. Patient data is encrypted at rest in line with RA 10173 (Data Privacy Act).

## Deployed URL

- **API:** https://urolens-api.azurewebsites.net
- **Interactive API docs:** https://urolens-api.azurewebsites.net/docs
- **Health check:** https://urolens-api.azurewebsites.net/health/ready
- Web portal: https://portal.urolens.app ([urolens-web](https://github.com/urolens-project/urolens-web)) · Mobile app: [urolens-mobile](https://github.com/urolens-project/urolens-mobile)

## Tech stack

- **Language / framework:** Python 3.13, FastAPI 0.136, Uvicorn, Pydantic 2
- **Database:** PostgreSQL on Supabase, SQLAlchemy 2 (async, asyncpg), Alembic migrations
- **Storage:** Supabase Storage (private bucket, signed URLs)
- **Security:** PyJWT, bcrypt, cryptography (Fernet encryption at rest)
- **AI:** [urolens-ai-engine](https://github.com/urolens-project/urolens-ai-engine) v0.2.1 (YOLOv8 on PyTorch CPU, input gate, Smart Diagnosis rule engine), installed from `requirements.txt`
- **Reports:** ReportLab (PDF results)
- **Quality:** pytest, Ruff, pre-commit
- **Deployment:** Docker, GitHub Actions, GitHub Container Registry, Azure App Service (Linux, B1)

## Environment variables (names only)

See `.env.example` for descriptions and placeholder values. Never commit a real `.env`.

| Group | Variables |
|---|---|
| Database and storage | `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_IMAGE_BUCKET` |
| Security | `JWT_SIGNING_KEY`, `JWT_ALGORITHM`, `JWT_EXPIRY_HOURS`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `MAX_FAILED_ATTEMPTS`, `ENCRYPTION_KEY`, `DEDUP_HASH_KEY` |
| AI engine | `AI_MODEL_VERSION`, `MODEL_WEIGHTS_PATH`, `GATE_WEIGHTS_PATH`, `RULE_ENGINE_CONFIG_PATH`, `CLASS_THRESHOLDS_PATH` (leave the four paths unset; the engine ships its own files) |
| Multi-field workflow | `FIELD_WORKFLOW_ENABLED`, `MIN_FIELDS_PER_SPECIMEN`, `FIELD_LOW_CONFIDENCE_THRESHOLD`, `FIELD_OUTLIER_MULTIPLIER`, `FIELD_OUTLIER_MIN_ABOVE` |
| Deployment (Azure) | `CORS_ALLOWED_ORIGINS`, `WEBSITES_PORT`, `WEBSITES_CONTAINER_START_TIME_LIMIT`, `TORCH_NUM_THREADS` |

---

## 🛠️ Backend Setup

### 📋 Prerequisites

- Python 3.13
- A PostgreSQL database (local instance or a Supabase project)

### 1️⃣ 📥 Clone and create a virtual environment

```bash
git clone https://github.com/urolens-project/urolens-backend.git
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
