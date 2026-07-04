"""
M4 API tests — the four endpoints flagged as "owed by M4" in
docs/API_CONTRACT.md:

  1. POST /expenses/upload           — create expense row + store PDF + enqueue
  2. GET  /expenses/{id}/pdf         — stream the stored PDF
  3. GET  /expenses/{id}/raw-extraction — expose raw_extraction JSONB
  4. PUT  /expenses/{id}/line-items  — needs_review -> parsed correction

The Celery broker is never touched: `get_extraction_enqueuer` is overridden
per-test with a coroutine that runs the M3 pipeline (`_process_with_session`)
inline against the same test engine, exactly like `tests/test_extraction_pipeline.py`
drives `_persist_pipeline_result` directly — no live Redis/Celery worker is
required (CLAUDE.md: SQLite tier needs no external services).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_db, get_extraction_enqueuer, get_storage
from app.extraction.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResult,
)
from app.extraction.tasks import _process_with_session
from app.main import app
from app.storage.local import LocalFilesystemStorage
from tests.auth_test_utils import attach_auto_auth

API = "/api/v1"
FIXTURES_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "invoices"


def _load_expected(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


class ScriptedProvider(ExtractionProvider):
    """Same test double as tests/test_extraction_pipeline.py."""

    name = "scripted"

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[ExtractionRequest] = []

    def is_configured(self) -> bool:
        return True

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        if not self._responses:
            return ExtractionResult(
                provider=self.name, raw=None, error="no more scripted responses"
            )
        raw = self._responses.pop(0)
        return ExtractionResult(provider=self.name, raw=raw)


# ---------------------------------------------------------------------------
# Fixture: API client wired to the test engine, with storage + Celery-enqueue
# both overridden so no external services are required.
# ---------------------------------------------------------------------------


@pytest.fixture
async def api(engine, tmp_path):
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    storage = LocalFilesystemStorage(tmp_path / "pdfs")

    # Mutable box so individual tests can opt into running the pipeline
    # inline (by setting .provider) or leave the expense truly 'queued'
    # (default — provider is None, enqueue is a no-op).
    state: dict[str, ExtractionProvider | None] = {"provider": None}
    enqueue_calls: list[tuple[uuid.UUID, str | None]] = []

    async def _override_get_db():
        async with factory() as session:
            yield session

    async def _override_get_storage():
        return storage

    async def _override_enqueue(
        expense_id: uuid.UUID, pdf_bytes: bytes, vendor_hint: str | None = None
    ) -> None:
        enqueue_calls.append((expense_id, vendor_hint))
        provider = state["provider"]
        if provider is None:
            return
        async with factory() as session:
            await _process_with_session(
                session, expense_id, pdf_bytes, provider, vendor_hint
            )

    async def _override_get_enqueue():
        return _override_enqueue

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_storage] = _override_get_storage
    app.dependency_overrides[get_extraction_enqueuer] = _override_get_enqueue

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        attach_auto_auth(ac)
        ac.extraction_state = state  # type: ignore[attr-defined]
        ac.enqueue_calls = enqueue_calls  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()


async def _make_user(client: AsyncClient, name: str) -> dict:
    resp = await client.post(
        f"{API}/users", json={"name": name, "email": f"{name}-{uuid.uuid4()}@x.com"}
    )
    assert resp.status_code == 201
    return resp.json()


def _token_for(user_id: str) -> str:
    """Mint a valid access token directly for a test-created user id."""
    from app.config import settings
    from app.domain.auth import create_access_token

    return create_access_token(uuid.UUID(user_id), settings.SECRET_KEY)


def _pdf_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


async def _upload(
    client: AsyncClient,
    paid_by: dict,
    filename: str = "amazon_good.pdf",
    group_id: str | None = None,
    vendor_hint: str | None = None,
) -> dict:
    files = {"file": (filename, _pdf_bytes(filename), "application/pdf")}
    data = {"paid_by": paid_by["id"]}
    if group_id is not None:
        data["group_id"] = group_id
    if vendor_hint is not None:
        data["vendor_hint"] = vendor_hint
    resp = await client.post(f"{API}/expenses/upload", data=data, files=files)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Upload endpoint
# ---------------------------------------------------------------------------


async def test_upload_creates_queued_expense_and_stores_pdf(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _upload(api, alice)

    assert expense["parse_status"] == "queued"
    assert expense["source"] == "pdf"
    assert expense["paid_by"] == alice["id"]
    assert expense["line_items"] == []
    assert expense["pdf_object_key"]
    # No pipeline ran (state["provider"] left None) — extraction not yet run,
    # matching API_CONTRACT.md §1: "Expense row created (PDF uploaded),
    # extraction not yet run."
    assert api.enqueue_calls == [(uuid.UUID(expense["id"]), None)]  # type: ignore[attr-defined]


async def test_upload_rejects_empty_file(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    files = {"file": ("empty.pdf", b"", "application/pdf")}
    resp = await api.post(
        f"{API}/expenses/upload", data={"paid_by": alice["id"]}, files=files
    )
    assert resp.status_code == 422


async def test_upload_rejects_non_pdf(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    files = {"file": ("notes.txt", b"hello world", "text/plain")}
    resp = await api.post(
        f"{API}/expenses/upload", data={"paid_by": alice["id"]}, files=files
    )
    assert resp.status_code == 422


async def test_upload_enforces_group_membership(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    # No group exists — any group_id is automatically "not a member".
    fake_group_id = str(uuid.uuid4())
    files = {"file": ("amazon_good.pdf", _pdf_bytes("amazon_good.pdf"), "application/pdf")}
    resp = await api.post(
        f"{API}/expenses/upload",
        data={"paid_by": alice["id"], "group_id": fake_group_id},
        files=files,
    )
    assert resp.status_code == 422


async def test_upload_pipeline_reaches_parsed(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    api.extraction_state["provider"] = ScriptedProvider(  # type: ignore[attr-defined]
        [_load_expected("amazon_good.expected.json")]
    )
    expense = await _upload(api, alice, filename="amazon_good.pdf", vendor_hint="Amazon")

    resp = await api.get(f"{API}/expenses/{expense['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_status"] == "parsed"
    assert body["total_minor"] == 150700
    assert len(body["line_items"]) > 0
    assert sum(li["total_minor"] for li in body["line_items"]) == 150700


async def test_upload_pipeline_reaches_needs_review(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    broken = _load_expected("zepto_broken.expected.json")
    api.extraction_state["provider"] = ScriptedProvider([broken, broken])  # type: ignore[attr-defined]
    expense = await _upload(api, alice, filename="zepto_broken.pdf", vendor_hint="Zepto")

    resp = await api.get(f"{API}/expenses/{expense['id']}")
    assert resp.status_code == 200
    assert resp.json()["parse_status"] == "needs_review"


# ---------------------------------------------------------------------------
# 2. PDF serving
# ---------------------------------------------------------------------------


async def test_get_pdf_roundtrips_uploaded_bytes(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _upload(api, alice, filename="amazon_good.pdf")

    resp = await api.get(f"{API}/expenses/{expense['id']}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _pdf_bytes("amazon_good.pdf")


async def test_get_pdf_404_when_no_pdf_stored(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    resp = await api.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 1000,
            "participants": [alice["id"]],
        },
    )
    assert resp.status_code == 201
    expense = resp.json()

    resp2 = await api.get(f"{API}/expenses/{expense['id']}/pdf")
    assert resp2.status_code == 404


async def test_get_pdf_404_unknown_expense(api: AsyncClient) -> None:
    # Auth now runs before existence is checked (an unauthenticated caller
    # must get 401, not a 404 that would leak "no such expense" info) --
    # authenticate as a real user first so this genuinely exercises the
    # "expense doesn't exist" 404 path.
    alice = await _make_user(api, "alice")
    resp = await api.get(
        f"{API}/expenses/{uuid.uuid4()}/pdf",
        headers={"Authorization": f"Bearer {_token_for(alice['id'])}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. raw_extraction exposure
# ---------------------------------------------------------------------------


async def test_raw_extraction_404_before_pipeline_runs(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _upload(api, alice)  # provider is None -> stays queued
    resp = await api.get(f"{API}/expenses/{expense['id']}/raw-extraction")
    assert resp.status_code == 404


async def test_raw_extraction_exposes_issues_for_needs_review(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    broken = _load_expected("zepto_broken.expected.json")
    api.extraction_state["provider"] = ScriptedProvider([broken, broken])  # type: ignore[attr-defined]
    expense = await _upload(api, alice, filename="zepto_broken.pdf")

    resp = await api.get(f"{API}/expenses/{expense['id']}/raw-extraction")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attempts"]) == 2
    last = body["attempts"][-1]
    assert last["validation"]["ok"] is False
    codes = {issue["code"] for issue in last["validation"]["issues"]}
    assert "invoice_total_mismatch" in codes
    assert last["raw"]["invoice_total_minor"] == 19000


# ---------------------------------------------------------------------------
# 4. Line-item correction (needs_review -> parsed)
# ---------------------------------------------------------------------------


async def _needs_review_expense(api: AsyncClient, alice: dict) -> dict:
    broken = _load_expected("zepto_broken.expected.json")
    api.extraction_state["provider"] = ScriptedProvider([broken, broken])  # type: ignore[attr-defined]
    expense = await _upload(api, alice, filename="zepto_broken.pdf")
    resp = await api.get(f"{API}/expenses/{expense['id']}")
    assert resp.json()["parse_status"] == "needs_review"
    return expense


async def test_correction_success_transitions_to_parsed(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _needs_review_expense(api, alice)

    # zepto_broken: items sum to 16500, real invoice total (per the last
    # extraction attempt) is 19000 — missing the 2500 delivery fee. The
    # correction UI's job is exactly this: add the missing row.
    resp = await api.put(
        f"{API}/expenses/{expense['id']}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Milk 1L",
                    "quantity": "2",
                    "unit_price_minor": 6000,
                    "total_minor": 12000,
                },
                {
                    "line_no": 2,
                    "kind": "item",
                    "description": "Bread",
                    "quantity": "1",
                    "unit_price_minor": 4500,
                    "total_minor": 4500,
                },
                {
                    "line_no": 3,
                    "kind": "delivery_fee",
                    "description": "Delivery",
                    "quantity": "1",
                    "unit_price_minor": 2500,
                    "total_minor": 2500,
                },
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "parsed"
    assert body["total_minor"] == 19000
    assert body["subtotal_minor"] == 16500
    assert sum(li["total_minor"] for li in body["line_items"]) == 19000

    # Now confirmable/splittable exactly like any other 'parsed' expense —
    # proves the correction didn't leave the expense in a half-wired state.
    resp2 = await api.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": li["id"], "user_id": alice["id"]}
                for li in body["line_items"]
            ]
        },
    )
    assert resp2.status_code == 200, resp2.text
    resp3 = await api.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["parse_status"] == "confirmed"


async def test_correction_still_mismatched_returns_422_with_issues(
    api: AsyncClient,
) -> None:
    alice = await _make_user(api, "alice")
    expense = await _needs_review_expense(api, alice)

    resp = await api.put(
        f"{API}/expenses/{expense['id']}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Milk 1L",
                    "total_minor": 12000,
                },
                {
                    "line_no": 2,
                    "kind": "item",
                    "description": "Bread",
                    "total_minor": 4500,
                },
            ]
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    codes = {issue["code"] for issue in detail["issues"]}
    assert "invoice_total_mismatch" in codes

    # Rejected correction must NOT have transitioned parse_status.
    resp2 = await api.get(f"{API}/expenses/{expense['id']}")
    assert resp2.json()["parse_status"] == "needs_review"


async def test_correction_rejected_when_not_needs_review(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _upload(api, alice)  # stays 'queued' (provider is None)

    resp = await api.put(
        f"{API}/expenses/{expense['id']}/line-items",
        json={
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "x", "total_minor": 100}
            ]
        },
    )
    assert resp.status_code == 409


async def test_correction_rejects_duplicate_line_no(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _needs_review_expense(api, alice)

    resp = await api.put(
        f"{API}/expenses/{expense['id']}/line-items",
        json={
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "a", "total_minor": 9500},
                {"line_no": 1, "kind": "item", "description": "b", "total_minor": 9500},
            ]
        },
    )
    assert resp.status_code == 422


async def test_correction_rejects_unknown_parent_line_no(api: AsyncClient) -> None:
    alice = await _make_user(api, "alice")
    expense = await _needs_review_expense(api, alice)

    resp = await api.put(
        f"{API}/expenses/{expense['id']}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "a",
                    "total_minor": 19000,
                    "parent_line_no": 99,
                },
            ]
        },
    )
    assert resp.status_code == 422


async def test_correction_404_unknown_expense(api: AsyncClient) -> None:
    # Any authenticated user may probe existence -- 404 vs 403 is decided
    # once the expense (and its owning group, if any) is actually found.
    alice = await _make_user(api, "alice")
    resp = await api.put(
        f"{API}/expenses/{uuid.uuid4()}/line-items",
        json={
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "x", "total_minor": 100}
            ]
        },
        headers={"Authorization": f"Bearer {_token_for(alice['id'])}"},
    )
    assert resp.status_code == 404
