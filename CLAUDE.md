# Splitr — Item-Level Expense Splitting App

## What this project is
Cross-platform (Web + Mobile) expense splitting app. Users upload invoice PDFs
(Amazon, Swiggy, Zomato, Zepto, Blinkit, etc.), the system extracts line items,
users assign items to people/subgroups, and a ledger computes who owes whom.

**Read `docs/ARCHITECTURE.md` before making any design decision. It is the
source of truth for the schema, PDF pipeline, and splitting algorithm.**

## Stack (fixed — do not substitute without asking the user)
- Backend: Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2
- Async jobs: Celery + Redis
- DB: PostgreSQL 15+ (money = BIGINT minor units, NEVER float)
- Web: Next.js 14+ (App Router) + TypeScript + Tailwind
- Mobile: React Native (Expo) + TypeScript
- Shared logic: `packages/core` (TS types, API client, zod schemas)
- PDF pipeline: pdfplumber + LLM structured outputs (provider behind an
  interface in `backend/app/extraction/providers/`, so Gemini/OpenAI/local
  are swappable). API keys via env vars only, never committed.

## Standing rules (do not re-litigate)
- **Environment / Docker:** Docker Desktop is installed on the D: drive and is
  ALWAYS already running. Never check C:\ paths, never attempt to start
  Docker, never ask if it's installed. The only permitted check is
  `docker ps`; if it returns, proceed directly to `docker compose up -d` and
  the Postgres suite.
- **Migrations:** Migration numbers are determined from `alembic heads` at
  pickup time, never from plan documents.

## Repo layout
```
backend/   FastAPI app, Celery workers, Alembic migrations, pytest
web/       Next.js app
mobile/    Expo app
packages/core/  shared TS types + API client (generated from OpenAPI)
docs/      ARCHITECTURE.md and ADRs
```

## Non-negotiable invariants
1. All money in integer minor units (paise). Assert
   `sum(shares) == expense.total_minor` before any ledger write.
2. Ledger is append-only. Never UPDATE/DELETE confirmed financial rows;
   corrections are new signed entries.
3. Largest-remainder rounding for all proportional splits (see ARCHITECTURE.md §4).
4. Every extraction result must pass the deterministic validation engine
   before status='parsed'; otherwise status='needs_review'.
5. Every backend feature ships with pytest tests; splitting/ledger code
   requires property-style tests (random carts must always reconcile).

## Conventions
- Conventional commits (feat:, fix:, test:, chore:)
- Backend: ruff + mypy strict on `app/domain/`
- Frontend: eslint + prettier defaults
- API is OpenAPI-first; regenerate `packages/core` client after route changes

## Build order (do not skip ahead)
1. M1: DB schema + migrations + ledger + settlement engine + manual expenses (API only)
2. M2: Splitting engine (items, fees, discounts, refunds) — fully tested
3. M3: PDF pipeline (text PDFs) + validation engine + needs_review flow
4. M4: Web UI (auth, groups, upload, assignment screen, balances)
5. M5: Mobile app reusing packages/core
6. M6: Vision path for image PDFs, vendor hints, debt simplification

## CI — Testing requirements (reviewer finding L5)

The test suite has two tiers:

**SQLite (default, fast, no services needed)**
```
cd backend && pytest
```
Covers all domain logic, rounding, API contracts, and ORM behaviour.
SQLite does NOT enforce CHECK constraints or support SELECT … FOR UPDATE, so
Postgres-specific tests are automatically skipped.

**PostgreSQL (required before merging M3+)**
```
# Start services (adjust docker path on Windows if needed):
docker compose up -d --wait
# Point tests at the test database:
TEST_DATABASE_URL=postgresql+psycopg://splitr:splitr@localhost:5435/splitr_test \
  pytest --tb=short
```
Tests decorated with `@pytest.mark.postgres` run only when `TEST_DATABASE_URL`
is a Postgres URL. They cover:
- C1: SELECT … FOR UPDATE atomicity (concurrent confirm race)
- H3: Postgres trigger guards on ledger_entries and expenses
- M2/M3: enforced CHECK constraints (amount_minor > 0, total_minor > 0)

**Before adding a new Postgres-only migration:** run `alembic upgrade head`
against the test database and confirm the full suite (both tiers) is green.
The Docker Compose file in the repo root provides a ready-to-use Postgres
container on port 5435 (avoids conflicts with local Postgres on 5432).
