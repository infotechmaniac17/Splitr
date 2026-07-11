"""
E2E automation: hits the real HTTP API exactly like the web UI does —
register a user, POST /expenses/upload a real Amazon PDF, then poll
GET /expenses/{id} until parse_status leaves 'queued'. Measures true
wall-clock time as experienced by a user watching the "processing" spinner.

Requires the full dev stack running (backend on :8000, Celery worker,
Postgres+Redis via docker compose) — see dev-up.bat.
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

API = "http://localhost:8000/api/v1"
POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 120.0


def main(pdf_path: str) -> None:
    email = f"e2e-{uuid.uuid4().hex[:8]}@test.local"
    with httpx.Client(base_url=API, timeout=30.0) as client:
        print(f"registering {email} ...")
        r = client.post(
            "/auth/register",
            json={"name": "E2E Test", "email": email, "password": "testpass123"},
        )
        r.raise_for_status()
        token_data = r.json()
        token = token_data["access_token"]
        user_id = token_data["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"registered user_id={user_id}")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        print(f"uploading {pdf_path} ({len(pdf_bytes)} bytes) ...")
        t_upload_start = time.time()
        r = client.post(
            "/expenses/upload",
            files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
            data={"paid_by": user_id},
            headers=headers,
        )
        r.raise_for_status()
        expense = r.json()
        expense_id = expense["id"]
        upload_elapsed = time.time() - t_upload_start
        print(f"upload accepted in {upload_elapsed:.2f}s, expense_id={expense_id}, "
              f"parse_status={expense['parse_status']}")

        print("polling for extraction to finish (this is what the UI spinner waits on)...")
        t_poll_start = time.time()
        last_status = expense["parse_status"]
        while True:
            elapsed = time.time() - t_poll_start
            if elapsed > POLL_TIMEOUT_S:
                print(f"TIMEOUT after {elapsed:.1f}s — still {last_status}")
                sys.exit(1)

            r = client.get(f"/expenses/{expense_id}", headers=headers)
            r.raise_for_status()
            expense = r.json()
            status = expense["parse_status"]
            if status != last_status:
                print(f"  [{elapsed:5.1f}s] status changed: {last_status} -> {status}")
                last_status = status
            if status not in ("queued", "processing"):
                break
            time.sleep(POLL_INTERVAL_S)

        total_elapsed = time.time() - t_upload_start
        print()
        print(f"=== RESULT ===")
        print(f"final parse_status: {last_status}")
        print(f"total wall time (upload -> final status): {total_elapsed:.2f}s")
        print(f"vendor: {expense.get('vendor')}")
        print(f"total_minor: {expense.get('total_minor')}")
        print(f"line_items: {len(expense.get('line_items', []))}")

        if last_status == "needs_review":
            r = client.get(f"/expenses/{expense_id}/raw-extraction", headers=headers)
            if r.status_code == 200:
                raw = r.json()
                print("raw_extraction:", raw)


if __name__ == "__main__":
    main(sys.argv[1])
