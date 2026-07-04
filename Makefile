# ---------------------------------------------------------------------------
# Splitr — root Makefile
# Run these targets from the repo root under Git Bash (or any POSIX shell).
#
# Prerequisites:
#   - Docker Desktop running (for `up`, `down`)
#   - backend/.venv created: python -m venv backend/.venv
#                            backend/.venv/Scripts/pip install -e "backend[dev]"
# ---------------------------------------------------------------------------

PYTHON   := backend/.venv/Scripts/python
ALEMBIC  := backend/.venv/Scripts/alembic
PYTEST   := backend/.venv/Scripts/pytest
RUFF     := backend/.venv/Scripts/ruff
MYPY     := backend/.venv/Scripts/mypy

# Fallback to `python` on the PATH if the venv doesn't exist yet (e.g. CI)
ifeq (,$(wildcard backend/.venv/Scripts/python))
  PYTHON  := python
  ALEMBIC := alembic
  PYTEST  := pytest
  RUFF    := ruff
  MYPY    := mypy
endif

.PHONY: up down test lint typecheck migrate revision seed help

## up: Start postgres + redis in the background (Docker required)
up:
	docker compose up -d --wait

## down: Stop and remove containers (volumes are preserved)
down:
	docker compose down

## test: Run the full pytest suite from backend/
test:
	cd backend && $(abspath $(PYTEST)) -v

## lint: Ruff check + auto-fix (safe fixes only)
lint:
	cd backend && $(abspath $(RUFF)) check . --fix
	cd backend && $(abspath $(RUFF)) format .

## typecheck: mypy on the entire app package
typecheck:
	cd backend && $(abspath $(MYPY)) app/

## migrate: Apply all pending Alembic migrations to the database
migrate:
	cd backend && $(abspath $(ALEMBIC)) upgrade head

## revision: Generate a new Alembic autogenerate migration
##   Usage: make revision MSG="add users table"
revision:
	cd backend && $(abspath $(ALEMBIC)) revision --autogenerate -m "$(MSG)"

## seed: Placeholder — backend engineer wires up seed data script here
seed:
	@echo "No seed script yet — implement backend/scripts/seed.py and wire it here."

## help: Show this help
help:
	@grep -E '^##' $(MAKEFILE_LIST) | sed 's/## //'
