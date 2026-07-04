"""
M2 API tests — item-level flow: line items → assignments → shares → confirm,
plus post-confirmation refunds.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

API = "/api/v1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(client: AsyncClient, name: str) -> dict:
    resp = await client.post(
        f"{API}/users", json={"name": name, "email": f"{name}-{uuid.uuid4()}@x.com"}
    )
    assert resp.status_code == 201
    return resp.json()


async def _make_group(client: AsyncClient, creator: dict, members: list[dict]) -> dict:
    resp = await client.post(
        f"{API}/groups", json={"name": "Test Group", "created_by": creator["id"]}
    )
    assert resp.status_code == 201
    group = resp.json()
    for member in members:
        if member["id"] == creator["id"]:
            continue
        resp2 = await client.post(
            f"{API}/groups/{group['id']}/members", json={"user_id": member["id"]}
        )
        assert resp2.status_code in (200, 201)
    return group


async def _worked_example_expense(
    client: AsyncClient, alice: dict, bob: dict, group: dict
) -> tuple[dict, dict[str, dict]]:
    """
    Create the ARCHITECTURE.md §4 worked example via the M2 flow:
    A=2000 items, B=4000, cart discount −1000, delivery fee +300, total 5300.
    Returns (expense_json, lines_by_description).
    """
    resp = await client.post(
        f"{API}/expenses",
        json={
            "group_id": group["id"],
            "paid_by": alice["id"],
            "vendor": "Swiggy",
            "total_minor": 5300,
            "line_items": [
                {"line_no": 1, "kind": "item", "description": "A items", "total_minor": 2000},
                {"line_no": 2, "kind": "item", "description": "B items", "total_minor": 4000},
                {
                    "line_no": 3,
                    "kind": "discount",
                    "description": "Coupon",
                    "total_minor": -1000,
                    "discount_scope": "cart",
                },
                {
                    "line_no": 4,
                    "kind": "delivery_fee",
                    "description": "Delivery",
                    "total_minor": 300,
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    expense = resp.json()
    lines = {li["description"]: li for li in expense["line_items"]}

    resp2 = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": lines["A items"]["id"], "user_id": alice["id"]},
                {"line_item_id": lines["B items"]["id"], "user_id": bob["id"]},
            ]
        },
    )
    assert resp2.status_code == 200, resp2.text
    return expense, lines


# ---------------------------------------------------------------------------
# Item-level flow
# ---------------------------------------------------------------------------


async def test_m2_full_flow_worked_example(client: AsyncClient) -> None:
    """Create → assign → preview → confirm; §4 numbers land in the ledger."""
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    group = await _make_group(client, alice, [alice, bob])
    expense, _ = await _worked_example_expense(client, alice, bob, group)

    # Preview shares.
    resp = await client.get(f"{API}/expenses/{expense['id']}/shares")
    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert shares[alice["id"]] == 1767
    assert shares[bob["id"]] == 3533

    # Confirm posts to ledger.
    resp2 = await client.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp2.status_code == 200
    assert resp2.json()["parse_status"] == "confirmed"

    # Bob owes Alice exactly his share.
    resp3 = await client.get(f"{API}/groups/{group['id']}/balances")
    balances = resp3.json()["balances"]
    assert balances == [
        {
            "debtor_id": bob["id"],
            "creditor_id": alice["id"],
            "net_amount_minor": 3533,
        }
    ]

    # Confirmed shares are frozen — preview now returns the same numbers.
    resp4 = await client.get(f"{API}/expenses/{expense['id']}/shares")
    assert resp4.json()["shares"][bob["id"]] == 3533


async def test_m2_weighted_assignment(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 900,
            "line_items": [
                {"line_no": 1, "kind": "item", "total_minor": 900},
            ],
        },
    )
    assert resp.status_code == 201
    expense = resp.json()
    line_id = expense["line_items"][0]["id"]

    resp2 = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": line_id, "user_id": alice["id"], "weight": "2"},
                {"line_item_id": line_id, "user_id": bob["id"], "weight": "1"},
            ]
        },
    )
    assert resp2.status_code == 200

    resp3 = await client.get(f"{API}/expenses/{expense['id']}/shares")
    shares = resp3.json()["shares"]
    assert shares[alice["id"]] == 600
    assert shares[bob["id"]] == 300


async def test_m2_line_totals_must_sum_to_total(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 1000,
            "line_items": [{"line_no": 1, "kind": "item", "total_minor": 900}],
        },
    )
    assert resp.status_code == 422


async def test_m2_discount_must_be_negative(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 1100,
            "line_items": [
                {"line_no": 1, "kind": "item", "total_minor": 1000},
                {"line_no": 2, "kind": "discount", "total_minor": 100},
            ],
        },
    )
    assert resp.status_code == 422


async def test_m2_confirm_without_assignments_rejected(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "line_items": [{"line_no": 1, "kind": "item", "total_minor": 500}],
        },
    )
    expense = resp.json()
    resp2 = await client.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp2.status_code == 422
    assert "assignments" in resp2.json()["detail"].lower()


async def test_m2_assignments_locked_after_confirm(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    group = await _make_group(client, alice, [alice, bob])
    expense, lines = await _worked_example_expense(client, alice, bob, group)
    await client.post(f"{API}/expenses/{expense['id']}/confirm")

    resp = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": lines["A items"]["id"], "user_id": bob["id"]},
            ]
        },
    )
    assert resp.status_code == 409


async def test_m2_assignment_to_foreign_line_rejected(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "line_items": [{"line_no": 1, "kind": "item", "total_minor": 500}],
        },
    )
    expense = resp.json()
    resp2 = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": str(uuid.uuid4()), "user_id": alice["id"]},
            ]
        },
    )
    assert resp2.status_code == 422


async def test_m2_non_member_assignment_rejected(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    outsider = await _make_user(client, "mallory")
    group = await _make_group(client, alice, [alice, bob])
    expense, lines = await _worked_example_expense(client, alice, bob, group)

    resp = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": lines["A items"]["id"], "user_id": outsider["id"]},
            ]
        },
    )
    assert resp.status_code == 422


async def test_m2_pre_confirmation_refund_line(client: AsyncClient) -> None:
    """Refund line in the original payload nets into the split (parent ratios)."""
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 2100,
            "line_items": [
                {"line_no": 1, "kind": "item", "total_minor": 3000},
                {
                    "line_no": 2,
                    "kind": "refund",
                    "total_minor": -900,
                    "parent_line_no": 1,
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    expense = resp.json()
    item_line = next(
        li for li in expense["line_items"] if li["kind"] == "item"
    )
    resp2 = await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": item_line["id"], "user_id": alice["id"], "weight": "2"},
                {"line_item_id": item_line["id"], "user_id": bob["id"], "weight": "1"},
            ]
        },
    )
    assert resp2.status_code == 200
    resp3 = await client.get(f"{API}/expenses/{expense['id']}/shares")
    shares = resp3.json()["shares"]
    # 3000 split 2:1 = 2000/1000; refund −900 follows 2:1 = −600/−300.
    assert shares[alice["id"]] == 1400
    assert shares[bob["id"]] == 700


# ---------------------------------------------------------------------------
# Post-confirmation refunds
# ---------------------------------------------------------------------------


async def test_m2_post_confirmation_refund_flow(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    group = await _make_group(client, alice, [alice, bob])
    expense, lines = await _worked_example_expense(client, alice, bob, group)
    resp = await client.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp.status_code == 200

    # Refund ₹6 (600) on B's item line — flows back 100% to Bob.
    resp2 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": lines["B items"]["id"], "amount_minor": 600},
    )
    assert resp2.status_code == 201, resp2.text
    refunded = resp2.json()
    refund_lines = [li for li in refunded["line_items"] if li["kind"] == "refund"]
    assert len(refund_lines) == 1
    assert refund_lines[0]["total_minor"] == -600
    assert refund_lines[0]["parent_line_id"] == lines["B items"]["id"]

    # Balance shifts: Bob owed 3533, refund reverses 600 → 2933.
    resp3 = await client.get(f"{API}/groups/{group['id']}/balances")
    balances = resp3.json()["balances"]
    assert balances == [
        {
            "debtor_id": bob["id"],
            "creditor_id": alice["id"],
            "net_amount_minor": 2933,
        }
    ]


async def test_m2_refund_shared_item_follows_ratios(client: AsyncClient) -> None:
    """Refund on a 2:1 shared line reverses 2:1."""
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 3000,
            "line_items": [{"line_no": 1, "kind": "item", "total_minor": 3000}],
        },
    )
    expense = resp.json()
    line_id = expense["line_items"][0]["id"]
    await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": line_id, "user_id": alice["id"], "weight": "2"},
                {"line_item_id": line_id, "user_id": bob["id"], "weight": "1"},
            ]
        },
    )
    await client.post(f"{API}/expenses/{expense['id']}/confirm")

    # Bob owes 1000. Refund 900 → Bob's reversal 300 → net 700.
    resp2 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": line_id, "amount_minor": 900},
    )
    assert resp2.status_code == 201, resp2.text

    resp3 = await client.get(f"{API}/users/{bob['id']}/balance")
    assert resp3.json()["net_balance_minor"] == -700


async def test_m2_refund_before_confirm_rejected(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 500,
            "line_items": [{"line_no": 1, "kind": "item", "total_minor": 500}],
        },
    )
    expense = resp.json()
    resp2 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={
            "parent_line_id": expense["line_items"][0]["id"],
            "amount_minor": 100,
        },
    )
    assert resp2.status_code == 409


async def test_m2_refund_cannot_exceed_line_total(client: AsyncClient) -> None:
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    group = await _make_group(client, alice, [alice, bob])
    expense, lines = await _worked_example_expense(client, alice, bob, group)
    await client.post(f"{API}/expenses/{expense['id']}/confirm")

    line_id = lines["B items"]["id"]
    # First refund OK.
    resp = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": line_id, "amount_minor": 3000},
    )
    assert resp.status_code == 201
    # Second refund would exceed the 4000 line total.
    resp2 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": line_id, "amount_minor": 1500},
    )
    assert resp2.status_code == 422


async def test_m2_refund_cap_uses_net_after_item_discount(
    client: AsyncClient,
) -> None:
    """Reviewer CRITICAL: cap must be net of item-scoped discounts.

    Item 1000 with item-scoped discount −200 → Alice actually paid 800.
    Refunding 1000 would manufacture 200 of debt from nowhere.
    """
    bob = await _make_user(client, "bob")
    alice = await _make_user(client, "alice")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": bob["id"],
            "total_minor": 800,
            "line_items": [
                {"line_no": 1, "kind": "item", "total_minor": 1000},
                {
                    "line_no": 2,
                    "kind": "discount",
                    "discount_scope": "item",
                    "parent_line_no": 1,
                    "total_minor": -200,
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    expense = resp.json()
    item_line = next(li for li in expense["line_items"] if li["kind"] == "item")
    await client.put(
        f"{API}/expenses/{expense['id']}/assignments",
        json={
            "assignments": [
                {"line_item_id": item_line["id"], "user_id": alice["id"]},
            ]
        },
    )
    resp2 = await client.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp2.status_code == 200

    # Gross refund (1000) must be rejected — only 800 was ever collected.
    resp3 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": item_line["id"], "amount_minor": 1000},
    )
    assert resp3.status_code == 422
    assert "net line total" in resp3.json()["detail"]

    # Net refund (800) is fine and zeroes the balance.
    resp4 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={"parent_line_id": item_line["id"], "amount_minor": 800},
    )
    assert resp4.status_code == 201, resp4.text
    resp5 = await client.get(f"{API}/users/{alice['id']}/balance")
    assert resp5.json()["net_balance_minor"] == 0


async def test_m2_refund_idempotency_key_prevents_double_post(
    client: AsyncClient,
) -> None:
    """Reviewer HIGH: retried refund with same key must not double-post."""
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    group = await _make_group(client, alice, [alice, bob])
    expense, lines = await _worked_example_expense(client, alice, bob, group)
    await client.post(f"{API}/expenses/{expense['id']}/confirm")

    body = {
        "parent_line_id": lines["B items"]["id"],
        "amount_minor": 600,
        "idempotency_key": "retry-abc-123",
    }
    resp1 = await client.post(f"{API}/expenses/{expense['id']}/refunds", json=body)
    assert resp1.status_code == 201, resp1.text
    # Identical retry: no new refund line, no new ledger entry.
    resp2 = await client.post(f"{API}/expenses/{expense['id']}/refunds", json=body)
    assert resp2.status_code == 201
    refund_lines = [
        li for li in resp2.json()["line_items"] if li["kind"] == "refund"
    ]
    assert len(refund_lines) == 1

    resp3 = await client.get(f"{API}/groups/{group['id']}/balances")
    assert resp3.json()["balances"][0]["net_amount_minor"] == 2933  # 3533 − 600

    # A DIFFERENT key posts a genuine second refund.
    resp4 = await client.post(
        f"{API}/expenses/{expense['id']}/refunds",
        json={**body, "idempotency_key": "retry-def-456"},
    )
    assert resp4.status_code == 201
    resp5 = await client.get(f"{API}/groups/{group['id']}/balances")
    assert resp5.json()["balances"][0]["net_amount_minor"] == 2333


async def test_m1_flow_still_works(client: AsyncClient) -> None:
    """Regression: the M1 participants flow is untouched."""
    alice = await _make_user(client, "alice")
    bob = await _make_user(client, "bob")
    resp = await client.post(
        f"{API}/expenses",
        json={
            "paid_by": alice["id"],
            "total_minor": 1000,
            "participants": [alice["id"], bob["id"]],
        },
    )
    assert resp.status_code == 201
    expense = resp.json()
    resp2 = await client.post(f"{API}/expenses/{expense['id']}/confirm")
    assert resp2.status_code == 200
    resp3 = await client.get(f"{API}/users/{bob['id']}/balance")
    assert resp3.json()["net_balance_minor"] == -500
