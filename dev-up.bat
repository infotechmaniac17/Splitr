@echo off
REM Splitr local dev stack — starts Postgres+Redis, runs migrations,
REM then opens 3 terminal windows for backend / celery / web.
REM Run from repo root (double-click or `dev-up.bat` from cmd).

setlocal
cd /d "%~dp0"

echo === [1/4] Starting Postgres + Redis (Docker) ===
docker compose up -d --wait
if errorlevel 1 (
    echo Docker compose failed — is Docker Desktop running?
    pause
    exit /b 1
)

echo === [2/4] Applying Alembic migrations ===
pushd backend
call .venv\Scripts\alembic upgrade head
if errorlevel 1 (
    echo Alembic migration failed.
    popd
    pause
    exit /b 1
)
popd

echo === Clearing stale process on port 8000 (if any) ===
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 .*LISTENING"') do (
    echo Killing stale PID %%a on port 8000
    taskkill /F /PID %%a >nul 2>&1
)

echo === [3/4] Launching backend API (port 8000) ===
start "Splitr Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python -m uvicorn app.main:app --reload --port 8000"

echo === [3/4] Launching Celery worker ===
start "Splitr Celery" cmd /k "cd /d %~dp0backend && .venv\Scripts\celery -A app.celery_app worker --loglevel=info --pool=solo"

echo === [4/4] Launching Next.js web app (port 3000) ===
start "Splitr Web" cmd /k "cd /d %~dp0 && npm run dev:web"

echo.
echo All services launching in separate windows:
echo   - Splitr Backend  -^> http://localhost:8000
echo   - Splitr Celery   -^> watch this window for extraction task logs
echo   - Splitr Web      -^> http://localhost:3000
echo.
echo Postgres/Redis running in Docker (docker compose ps to check).
endlocal
