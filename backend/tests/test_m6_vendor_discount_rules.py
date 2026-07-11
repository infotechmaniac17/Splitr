"""
M6 item 3: vendor discount rules.

Covers (per the spec):
  - Every CHECK constraint on vendor_discount_rules and expenses
    (@pytest.mark.postgres -- raw SQL, since SQLite does not enforce CHECK).
  - Threshold boundary (35000 applies, 34999 does not) using the canonical
    Amazon example (min_order_total_minor=35000, flat 5000).
  - Precedence: group rule beats global even when global's discount is
    larger; larger discount wins among same-scope rules; deterministic
    tie-break by lowest rule id when discounts are equal.
  - A rule edited/deactivated after an expense already snapshotted it does
    not change that expense's frozen discount_* columns.
  - Auto-apply skips drafts that already have discount_source='manual'.
  - Confirmed-expense discount column mutation rejected via both raw SQL
    and ORM session.commit() (@pytest.mark.postgres -- needs the DB
    trigger).
  - Percent rule capped at subtotal (never exceeds subtotal_minor).

Pure-function tests (match_rule, compute_discount_amount) run on both
SQLite and Postgres tiers since they touch no DB-enforced constraint.
CHECK-constraint and trigger tests are Postgres-only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DiscountSource,
    DiscountType,
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    Group,
    GroupMember,
    GroupMemberRole,
    ParseStatus,
    User,
    VendorDiscountRule,
)
from app.domain.vendor_discount import (
    apply_vendor_discount_snapshot,
    compute_discount_amount,
    find_matching_rule,
    match_rule,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirror tests/test_hardening.py / test_m6_item_assignment_
# confirm_guard.py)
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, name: str, email: str | None = None) -> User:
    user = User(
        name=name, email=email or f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com"
    )
    db.add(user)
    await db.flush()
    return user


async def _make_group(db: AsyncSession, creator: User) -> Group:
    group = Group(name="Vendor Discount Test Group", created_by=creator.id)
    db.add(group)
    await db.flush()
    db.add(
        GroupMember(group_id=group.id, user_id=creator.id, role=GroupMemberRole.admin)
    )
    await db.flush()
    return group


async def _make_expense(
    db: AsyncSession,
    payer: User,
    group: Group | None = None,
    total: int = 100000,
    subtotal: int | None = None,
    vendor: str | None = "Amazon",
    parse_status: ParseStatus = ParseStatus.parsed,
) -> Expense:
    expense = Expense(
        group_id=group.id if group else None,
        paid_by=payer.id,
        vendor=vendor,
        currency="INR",
        total_minor=total,
        subtotal_minor=subtotal if subtotal is not None else total,
        source=ExpenseSource.manual,
        parse_status=parse_status,
        status=ExpenseStatus.active,
    )
    db.add(expense)
    await db.flush()
    return expense


def _make_rule(
    *,
    group_id: uuid.UUID | None,
    created_by: uuid.UUID,
    vendor_pattern: str = "amazon",
    min_order_total_minor: int = 0,
    discount_type: DiscountType = DiscountType.flat,
    discount_value_minor: int | None = 5000,
    discount_percent: Decimal | None = None,
    rule_id: uuid.UUID | None = None,
    active: bool = True,
) -> VendorDiscountRule:
    return VendorDiscountRule(
        id=rule_id or uuid.uuid4(),
        group_id=group_id,
        created_by=created_by,
        vendor_pattern=vendor_pattern,
        min_order_total_minor=min_order_total_minor,
        discount_type=discount_type,
        discount_value_minor=discount_value_minor,
        discount_percent=discount_percent,
        active=active,
    )


# ---------------------------------------------------------------------------
# 1. Threshold boundary — canonical Amazon example.
# ---------------------------------------------------------------------------


def test_threshold_boundary_exact_value_applies() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        vendor_pattern="amazon",
        min_order_total_minor=35000,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    result = match_rule("amazon", 35000, None, [rule])
    assert result is rule


def test_threshold_boundary_one_below_does_not_apply() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        vendor_pattern="amazon",
        min_order_total_minor=35000,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    result = match_rule("amazon", 34999, None, [rule])
    assert result is None


# ---------------------------------------------------------------------------
# 2. Precedence: scope, larger discount, deterministic tie-break.
# ---------------------------------------------------------------------------


def test_group_rule_beats_global_even_when_global_is_larger() -> None:
    creator = uuid.uuid4()
    group_id = uuid.uuid4()
    global_rule = _make_rule(
        group_id=None,
        created_by=creator,
        discount_type=DiscountType.flat,
        discount_value_minor=100000,  # much larger
    )
    group_rule = _make_rule(
        group_id=group_id,
        created_by=creator,
        discount_type=DiscountType.flat,
        discount_value_minor=100,  # tiny
    )
    result = match_rule("amazon", 50000, group_id, [global_rule, group_rule])
    assert result is group_rule


def test_larger_discount_wins_among_same_scope_rules() -> None:
    creator = uuid.uuid4()
    small = _make_rule(group_id=None, created_by=creator, discount_value_minor=1000)
    large = _make_rule(group_id=None, created_by=creator, discount_value_minor=9000)
    result = match_rule("amazon", 50000, None, [small, large])
    assert result is large


def test_equal_discount_tie_break_by_lowest_uuid() -> None:
    creator = uuid.uuid4()
    id_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
    rule_a = _make_rule(
        group_id=None, created_by=creator, discount_value_minor=5000, rule_id=id_a
    )
    rule_b = _make_rule(
        group_id=None, created_by=creator, discount_value_minor=5000, rule_id=id_b
    )
    result = match_rule("amazon", 50000, None, [rule_b, rule_a])
    assert result.id == id_a  # lowest UUID wins deterministically


def test_only_global_rules_present_still_applies() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(group_id=None, created_by=creator, discount_value_minor=5000)
    result = match_rule("amazon", 50000, uuid.uuid4(), [rule])
    assert result is rule


def test_inactive_rule_never_matches() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(group_id=None, created_by=creator, active=False)
    result = match_rule("amazon", 50000, None, [rule])
    assert result is None


def test_vendor_pattern_mismatch_never_matches() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        vendor_pattern="swiggy",
        discount_value_minor=1000,
    )
    # Exact-equality matching only -- no substring containment (see
    # app/domain/vendor_discount.py module docstring).
    result = match_rule("amazon", 50000, None, [rule])
    assert result is None
    result2 = match_rule("swigg", 50000, None, [rule])  # substring, not exact
    assert result2 is None


async def test_find_matching_rule_db_query_prefers_group_scope(
    db_session: AsyncSession,
) -> None:
    """End-to-end through the async DB-query wrapper (not just the pure
    function): persists a global + a group rule and confirms the group rule
    is loaded and selected, even though the global rule's discount is
    larger."""
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)

    global_rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=100000,
    )
    group_rule = VendorDiscountRule(
        group_id=group.id,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=100,
    )
    db_session.add_all([global_rule, group_rule])
    await db_session.commit()

    result = await find_matching_rule(db_session, "amazon", 50000, group.id)
    assert result is not None
    assert result.id == group_rule.id


async def test_find_matching_rule_db_query_falls_back_to_global(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    group = await _make_group(db_session, alice)

    global_rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(global_rule)
    await db_session.commit()

    result = await find_matching_rule(db_session, "amazon", 50000, group.id)
    assert result is not None
    assert result.id == global_rule.id


# ---------------------------------------------------------------------------
# 3. Percent rule capped at subtotal.
# ---------------------------------------------------------------------------


def test_percent_discount_never_exceeds_subtotal() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        discount_type=DiscountType.percent,
        discount_value_minor=None,
        discount_percent=Decimal("100.00"),
    )
    amount = compute_discount_amount(rule, 12345)
    assert amount == 12345
    assert amount <= 12345


def test_percent_discount_rounds_half_even() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        discount_type=DiscountType.percent,
        discount_value_minor=None,
        discount_percent=Decimal("50.00"),
    )
    # 50% of an odd amount: 101 * 0.5 = 50.5 -> round-half-even -> 50
    assert compute_discount_amount(rule, 101) == 50
    # 50% of 103 = 51.5 -> round-half-even -> 52
    assert compute_discount_amount(rule, 103) == 52


def test_flat_discount_capped_at_subtotal() -> None:
    creator = uuid.uuid4()
    rule = _make_rule(
        group_id=None,
        created_by=creator,
        discount_type=DiscountType.flat,
        discount_value_minor=99999,
    )
    assert compute_discount_amount(rule, 500) == 500


# ---------------------------------------------------------------------------
# 4. Auto-apply skips drafts with discount_source='manual'.
# ---------------------------------------------------------------------------


async def test_auto_apply_skips_manual_discount_source(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    expense.discount_source = DiscountSource.manual
    expense.discount_type = DiscountType.flat
    expense.discount_value_minor = 1
    await db_session.flush()

    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    await apply_vendor_discount_snapshot(db_session, expense)

    # Manual snapshot must be untouched.
    assert expense.discount_source == DiscountSource.manual
    assert expense.discount_value_minor == 1
    assert expense.discount_rule_id is None


async def test_auto_apply_sets_vendor_rule_snapshot_on_draft(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)

    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=35000,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    await apply_vendor_discount_snapshot(db_session, expense)

    assert expense.discount_source == DiscountSource.vendor_rule
    assert expense.discount_rule_id == rule.id
    assert expense.discount_type == DiscountType.flat
    assert expense.discount_value_minor == 5000
    assert expense.discount_threshold_minor == 35000


async def test_auto_apply_never_touches_confirmed_expense(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(
        db_session, alice, subtotal=50000, parse_status=ParseStatus.confirmed
    )
    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    await apply_vendor_discount_snapshot(db_session, expense)

    assert expense.discount_source is None
    assert expense.discount_rule_id is None


async def test_auto_apply_overwrites_prior_extracted_snapshot(
    db_session: AsyncSession,
) -> None:
    """Precedence: vendor_rule wins over a prior 'extracted' snapshot."""
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    expense.discount_source = DiscountSource.extracted
    expense.discount_type = DiscountType.flat
    expense.discount_value_minor = 200
    await db_session.flush()

    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    await apply_vendor_discount_snapshot(db_session, expense)

    assert expense.discount_source == DiscountSource.vendor_rule
    assert expense.discount_value_minor == 5000
    assert expense.discount_rule_id == rule.id


# ---------------------------------------------------------------------------
# 5. Historical snapshot immutability: editing/deactivating a rule after an
#    expense snapshotted it does not change that expense.
# ---------------------------------------------------------------------------


async def test_rule_edit_after_snapshot_does_not_alter_historical_expense(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)

    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=35000,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.commit()

    await apply_vendor_discount_snapshot(db_session, expense)
    await db_session.commit()

    original_value = expense.discount_value_minor
    original_type = expense.discount_type
    original_threshold = expense.discount_threshold_minor
    assert original_value == 5000

    # Edit the rule (bigger discount) and deactivate it.
    rule.discount_value_minor = 99999
    rule.min_order_total_minor = 1
    rule.active = False
    await db_session.commit()

    reloaded = await db_session.get(Expense, expense.id)
    assert reloaded is not None
    assert reloaded.discount_value_minor == original_value == 5000
    assert reloaded.discount_type == original_type
    assert reloaded.discount_threshold_minor == original_threshold == 35000
    assert reloaded.discount_rule_id == rule.id  # lineage preserved


# ---------------------------------------------------------------------------
# 6. CHECK constraints — vendor_discount_rules (Postgres only).
# ---------------------------------------------------------------------------


async def _insert_rule_raw(
    db: AsyncSession,
    *,
    group_id: uuid.UUID | None,
    created_by: uuid.UUID,
    vendor_pattern: str = "amazon",
    min_order_total_minor: int = 0,
    discount_type: str = "flat",
    discount_value_minor: int | None = 5000,
    discount_percent: Decimal | None = None,
) -> None:
    await db.execute(
        sa.text(
            "INSERT INTO vendor_discount_rules "
            "(id, group_id, created_by, vendor_pattern, min_order_total_minor, "
            "discount_type, discount_value_minor, discount_percent, active, "
            "created_at, updated_at) "
            "VALUES (:id, :group_id, :created_by, :vendor_pattern, "
            ":min_order_total_minor, :discount_type, :discount_value_minor, "
            ":discount_percent, true, now(), now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "group_id": str(group_id) if group_id else None,
            "created_by": str(created_by),
            "vendor_pattern": vendor_pattern,
            "min_order_total_minor": min_order_total_minor,
            "discount_type": discount_type,
            "discount_value_minor": discount_value_minor,
            "discount_percent": discount_percent,
        },
    )
    await db.flush()


@pytest.mark.postgres
async def test_ck_rule_both_flat_and_percent_set_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            discount_type="flat",
            discount_value_minor=5000,
            discount_percent=Decimal("10.00"),
        )


@pytest.mark.postgres
async def test_ck_rule_percent_over_100_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            discount_type="percent",
            discount_value_minor=None,
            discount_percent=Decimal("100.01"),
        )


@pytest.mark.postgres
async def test_ck_rule_percent_zero_or_negative_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            discount_type="percent",
            discount_value_minor=None,
            discount_percent=Decimal("0.00"),
        )


@pytest.mark.postgres
async def test_ck_rule_flat_zero_or_negative_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            discount_type="flat",
            discount_value_minor=0,
            discount_percent=None,
        )


@pytest.mark.postgres
async def test_ck_rule_min_order_negative_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            min_order_total_minor=-1,
        )


@pytest.mark.postgres
async def test_ck_rule_invalid_discount_type_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await _insert_rule_raw(
            db_session,
            group_id=None,
            created_by=alice.id,
            discount_type="bogus",
        )


@pytest.mark.postgres
async def test_valid_flat_rule_insert_succeeds(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    await _insert_rule_raw(db_session, group_id=None, created_by=alice.id)
    await db_session.commit()


@pytest.mark.postgres
async def test_valid_percent_rule_insert_succeeds(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    await db_session.commit()
    await _insert_rule_raw(
        db_session,
        group_id=None,
        created_by=alice.id,
        discount_type="percent",
        discount_value_minor=None,
        discount_percent=Decimal("15.00"),
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# 7. CHECK constraints — expenses.discount_type / discount_source
#    (Postgres only).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_ck_expense_invalid_discount_type_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET discount_type = 'bogus' WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_ck_expense_invalid_discount_source_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET discount_source = 'bogus' WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


# ---------------------------------------------------------------------------
# 8. Confirmed-expense discount mutation rejected (trigger, Postgres only).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_confirmed_expense_discount_mutation_rejected_raw_sql(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    expense.discount_source = DiscountSource.vendor_rule
    expense.discount_type = DiscountType.flat
    expense.discount_value_minor = 5000
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET discount_value_minor = 1 WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_confirmed_expense_discount_mutation_rejected_orm(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    expense.discount_source = DiscountSource.vendor_rule
    expense.discount_type = DiscountType.flat
    expense.discount_value_minor = 5000
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    expense.discount_value_minor = 999
    with pytest.raises((DBAPIError, IntegrityError)):
        await db_session.commit()


@pytest.mark.postgres
async def test_confirmed_expense_discount_rule_id_mutation_rejected(
    db_session: AsyncSession,
) -> None:
    """discount_rule_id itself (lineage FK, not just the money columns) is
    also frozen once confirmed."""
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    rule = VendorDiscountRule(
        group_id=None,
        created_by=alice.id,
        vendor_pattern="amazon",
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
    )
    db_session.add(rule)
    await db_session.flush()
    expense.discount_source = DiscountSource.vendor_rule
    expense.discount_type = DiscountType.flat
    expense.discount_value_minor = 5000
    expense.discount_rule_id = rule.id
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET discount_rule_id = NULL WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_draft_expense_discount_columns_remain_mutable(
    db_session: AsyncSession,
) -> None:
    """Regression: the new guard must not over-block drafts."""
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, subtotal=50000)
    await db_session.commit()

    await db_session.execute(
        sa.text(
            "UPDATE expenses SET discount_value_minor = 123, "
            "discount_type = 'flat' WHERE id = :id"
        ),
        {"id": str(expense.id)},
    )
    await db_session.commit()
    await db_session.refresh(expense)

    assert expense.discount_value_minor == 123


# ---------------------------------------------------------------------------
# 9. Follow-up fix: _persist_pipeline_result must gate
#    apply_vendor_discount_snapshot on the expense's ORIGINAL parse_status
#    (captured before it gets overwritten), not the post-mutation value.
# ---------------------------------------------------------------------------


async def test_persist_pipeline_result_never_calls_snapshot_for_confirmed_expense(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Fast (SQLite + Postgres) companion to the Postgres-only test below.
    Verifies the app-level logic in isolation, with
    apply_vendor_discount_snapshot mocked out, so this half of the coverage
    doesn't require a live trigger. The Postgres-only test below is the one
    that actually proves the DB-level hazard is closed.
    """
    import app.extraction.tasks as tasks_module
    from app.extraction.pipeline import PipelineResult
    from app.extraction.schema import ExtractedInvoice

    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(
        db_session,
        alice,
        subtotal=50000,
        total=50000,
        parse_status=ParseStatus.confirmed,
    )
    await db_session.commit()

    assert expense.parse_status == ParseStatus.confirmed

    pre_parse_status = expense.parse_status
    pre_raw_extraction = expense.raw_extraction
    pre_vendor = expense.vendor
    pre_invoice_number = expense.invoice_number
    pre_currency = expense.currency
    pre_invoice_date = expense.invoice_date
    pre_subtotal_minor = expense.subtotal_minor
    pre_total_minor = expense.total_minor

    pipeline_result = PipelineResult(
        parse_status=ParseStatus.parsed,
        raw_extraction={"vendor": "Amazon"},
        invoice=ExtractedInvoice(
            vendor="Amazon",
            invoice_total_minor=50000,
            subtotal_minor=50000,
            line_items=[],
        ),
        route="text",
    )

    calls: list[Expense] = []

    async def _spy_apply_snapshot(db: AsyncSession, exp: Expense) -> None:
        calls.append(exp)

    monkeypatch.setattr(
        tasks_module, "apply_vendor_discount_snapshot", _spy_apply_snapshot
    )

    existing_line_items = (
        (
            await db_session.execute(
                sa.select(ExpenseLineItem).where(
                    ExpenseLineItem.expense_id == expense.id
                )
            )
        )
        .scalars()
        .all()
    )

    await tasks_module._persist_pipeline_result(db_session, expense, pipeline_result)

    assert calls == [], (
        "apply_vendor_discount_snapshot must never be called for an "
        "expense whose ORIGINAL parse_status was 'confirmed'."
    )

    assert expense.parse_status == pre_parse_status == ParseStatus.confirmed
    assert expense.raw_extraction == pre_raw_extraction
    assert expense.vendor == pre_vendor
    assert expense.invoice_number == pre_invoice_number
    assert expense.currency == pre_currency
    assert expense.invoice_date == pre_invoice_date
    assert expense.subtotal_minor == pre_subtotal_minor
    assert expense.total_minor == pre_total_minor

    assert expense not in db_session.dirty

    reloaded_line_items = (
        (
            await db_session.execute(
                sa.select(ExpenseLineItem).where(
                    ExpenseLineItem.expense_id == expense.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {li.id for li in reloaded_line_items} == {
        li.id for li in existing_line_items
    }

    await db_session.commit()


@pytest.mark.postgres
async def test_persist_pipeline_result_confirmed_expense_survives_real_trigger(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Postgres-only regression for the masked-guard finding, run against the
    REAL guard_expense_financial_immutability trigger (app/domain/
    pg_guards.py) -- NOT mocked out.

    IMPORTANT: SQLite cannot catch this class of bug. SQLite does not
    enforce triggers or CHECK constraints, so a version of
    `_persist_pipeline_result` that mutates `expense.parse_status` (and
    friends) before checking `original_status` would mutate the ORM object
    "successfully" and silently on SQLite, and this test would pass
    vacuously on that tier even with the bug present. Do NOT downgrade or
    relocate this test to the SQLite-only tier -- it only has teeth against
    a live Postgres trigger, which is exactly why it is marked
    @pytest.mark.postgres and skipped when TEST_DATABASE_URL is not a
    postgresql+psycopg:// URL (see tests/conftest.py).

    A prior version of this guard only gated the
    apply_vendor_discount_snapshot() call, while `_persist_pipeline_result`
    unconditionally overwrote expense.parse_status/raw_extraction/vendor/
    invoice_number/etc. on the ORM object *before* that call was ever
    reached. SQLAlchemy's autoflush sent that UPDATE to the DB as soon as
    the next query ran, and the trigger rejected it with an unhandled
    ProgrammingError -- 'confirmed' is terminal and can never change.

    This test drives `_persist_pipeline_result` for real (no mocking of
    apply_vendor_discount_snapshot, so the discount_* snapshot path is also
    exercised end-to-end) against an expense that starts out `confirmed`,
    with the real trigger DDL installed (via the db_session/engine
    fixtures), and asserts:
      (a) no DB exception is raised -- including on an explicit commit()
          afterwards, which is where the bug used to surface via autoflush;
      (b) EVERY field the function could have touched -- parse_status,
          invoice_number, raw_extraction, vendor, currency, invoice_date,
          subtotal_minor, total_minor, and all discount_* columns -- is
          byte-identical before and after the call;
      (c) a warning-level log naming the expense id was actually emitted
          (a logged skip, not a silent no-op).
    """
    import logging

    import app.extraction.tasks as tasks_module
    from app.extraction.pipeline import PipelineResult
    from app.extraction.schema import ExtractedInvoice

    alice = await _make_user(db_session, "Alice")

    # Build the expense directly (rather than via _make_expense + a
    # follow-up mutate-and-commit) so the discount_* snapshot fields are
    # part of the initial INSERT. trg_expense_immutability only fires
    # BEFORE UPDATE OR DELETE, not INSERT, so this sets up a pre-existing
    # (already-frozen) discount snapshot on an expense that is confirmed
    # from the moment it is first committed -- without itself triggering
    # the very guard this test exists to exercise.
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=50000,
        subtotal_minor=50000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.confirmed,
        status=ExpenseStatus.active,
        discount_source=DiscountSource.manual,
        discount_type=DiscountType.flat,
        discount_value_minor=1234,
        discount_percent=None,
        discount_threshold_minor=None,
    )
    db_session.add(expense)
    await db_session.commit()
    await db_session.refresh(expense)

    assert expense.parse_status == ParseStatus.confirmed

    pre = {
        "parse_status": expense.parse_status,
        "raw_extraction": expense.raw_extraction,
        "vendor": expense.vendor,
        "invoice_number": expense.invoice_number,
        "currency": expense.currency,
        "invoice_date": expense.invoice_date,
        "subtotal_minor": expense.subtotal_minor,
        "total_minor": expense.total_minor,
        "discount_source": expense.discount_source,
        "discount_type": expense.discount_type,
        "discount_value_minor": expense.discount_value_minor,
        "discount_percent": expense.discount_percent,
        "discount_threshold_minor": expense.discount_threshold_minor,
    }

    pipeline_result = PipelineResult(
        parse_status=ParseStatus.parsed,
        raw_extraction={"vendor": "Amazon", "attempted": True},
        invoice=ExtractedInvoice(
            vendor="Amazon Updated",
            invoice_number="ATTEMPTED-INV-001",
            invoice_total_minor=99999,
            subtotal_minor=99999,
            line_items=[],
        ),
        route="text",
    )

    with caplog.at_level(logging.WARNING, logger=tasks_module.logger.name):
        await tasks_module._persist_pipeline_result(
            db_session, expense, pipeline_result
        )

    # (c) logged skip, not a silent no-op.
    assert any(
        record.levelno == logging.WARNING and str(expense.id) in record.getMessage()
        for record in caplog.records
    ), "expected a WARNING-level log naming the confirmed expense's id"

    # (b) byte-identical for every field the function could have touched.
    post = {
        "parse_status": expense.parse_status,
        "raw_extraction": expense.raw_extraction,
        "vendor": expense.vendor,
        "invoice_number": expense.invoice_number,
        "currency": expense.currency,
        "invoice_date": expense.invoice_date,
        "subtotal_minor": expense.subtotal_minor,
        "total_minor": expense.total_minor,
        "discount_source": expense.discount_source,
        "discount_type": expense.discount_type,
        "discount_value_minor": expense.discount_value_minor,
        "discount_percent": expense.discount_percent,
        "discount_threshold_minor": expense.discount_threshold_minor,
    }
    assert post == pre

    # (a) no DB exception is raised -- neither during the call above nor on
    # an explicit commit(): the real trigger is installed and would reject
    # any attempted transition out of 'confirmed', but the guard prevents
    # the write from ever being attempted in the first place.
    await db_session.commit()
    await db_session.refresh(expense)
    assert expense.parse_status == ParseStatus.confirmed


# ---------------------------------------------------------------------------
# 10. M6 item 3 follow-up (MEDIUM, docs-as-tripwire): discount snapshot has
#     zero effect on computed shares/balances today.
# ---------------------------------------------------------------------------


def test_discount_snapshot_now_changes_allocation() -> None:
    """
    Flipped by M6 item 5 -- the discount snapshot now feeds allocation via
    compute_allocation; this deliberately asserts the opposite of the item-3
    invariant. Do not silently delete/rewrite without reading why.

    Original (item-3) tripwire, now historical: `compute_shares` itself
    takes only line items + total_minor and STILL has zero reference to an
    expense's discount_* fields -- that part of the original assertion
    remains literally true forever (compute_shares is never modified, per
    CLAUDE.md/M6 item 5's design). What changed is that item 5 introduced a
    NEW function, `compute_allocation`, that sits ON TOP of compute_shares
    and DOES read the discount snapshot (via discount_spec_from_expense) to
    layer a real discount into each member's final owed amount. Confirming
    an expense now goes through compute_allocation, not bare compute_shares
    (see app/api/expenses.py:confirm_expense) -- so the SAME snapshot that
    item 3 proved was allocation-inert is, as of item 5, exactly what makes
    two otherwise-identical expenses' allocations DIFFER.
    """
    from fractions import Fraction

    from app.domain.models import LineItemKind
    from app.domain.splitting import (
        DiscountSpec,
        LineInput,
        compute_allocation,
        discount_spec_from_expense,
    )

    alice_id = uuid.uuid4()
    bob_id = uuid.uuid4()
    line_id = uuid.uuid4()

    def _lines() -> list[LineInput]:
        return [
            LineInput(
                line_id=line_id,
                kind=LineItemKind.item,
                total_minor=50000,
                assignments=(
                    (alice_id, Fraction(1)),
                    (bob_id, Fraction(1)),
                ),
            )
        ]

    # Expense A: no discount snapshot at all.
    result_no_discount = compute_allocation(_lines(), 50000)
    shares_no_discount = {
        uid: b.total_minor for uid, b in result_no_discount.members.items()
    }

    # Expense B: the SAME line items/total, with a discount snapshot applied
    # to the parent Expense object -- compute_allocation DOES read this via
    # discount_spec_from_expense, unlike compute_shares.
    expense_with_discount = Expense(
        paid_by=alice_id,
        currency="INR",
        total_minor=50000,
        subtotal_minor=50000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.parsed,
        status=ExpenseStatus.active,
        discount_source=DiscountSource.vendor_rule,
        discount_type=DiscountType.flat,
        discount_value_minor=5000,
        discount_threshold_minor=0,
    )
    assert expense_with_discount.discount_value_minor == 5000  # sanity

    discount = discount_spec_from_expense(expense_with_discount)
    assert discount == DiscountSpec(
        type=DiscountType.flat, value_minor=5000, percent=None, threshold_minor=0
    )

    result_with_discount = compute_allocation(_lines(), 50000, discount=discount)
    shares_with_discount = {
        uid: b.total_minor for uid, b in result_with_discount.members.items()
    }

    # The whole point of this flip: allocation now DIFFERS with vs. without
    # the snapshot.
    assert shares_no_discount != shares_with_discount
    assert sum(shares_no_discount.values()) == 50000
    assert sum(shares_with_discount.values()) == 50000 - 5000 == 45000
    assert result_with_discount.applied_discount_minor == 5000


# ---------------------------------------------------------------------------
# 11. LOW fix: pin "group-rule-shadows-global-with-no-fallback" precedence.
# ---------------------------------------------------------------------------


def test_group_rule_wins_even_when_applicable_but_smaller_than_global() -> None:
    """
    Spec decision, not a bug: match_rule's documented precedence is that a
    group-scoped rule beats ALL global rules outright once it applies at
    all -- there is no cross-scope "pick whichever discount is bigger"
    comparison. This test constructs a case where the group rule IS
    genuinely applicable (subtotal clears its own threshold) but computes a
    strictly SMALLER discount than an applicable global rule for the same
    vendor, and asserts the group rule still wins.

    Do not "fix" this to compare group vs. global by discount magnitude
    without a deliberate spec change -- see the module docstring in
    app/domain/vendor_discount.py ("Scope precedence") for the rationale.
    """
    creator = uuid.uuid4()
    group_id = uuid.uuid4()

    global_rule = _make_rule(
        group_id=None,
        created_by=creator,
        min_order_total_minor=0,
        discount_type=DiscountType.flat,
        discount_value_minor=20000,  # large global discount
    )
    group_rule = _make_rule(
        group_id=group_id,
        created_by=creator,
        min_order_total_minor=10000,  # applicable: subtotal (50000) clears this
        discount_type=DiscountType.flat,
        discount_value_minor=500,  # much smaller than the global rule
    )

    result = match_rule("amazon", 50000, group_id, [global_rule, group_rule])

    assert result is group_rule
    assert compute_discount_amount(group_rule, 50000) < compute_discount_amount(
        global_rule, 50000
    )
