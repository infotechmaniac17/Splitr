"""
M6 item 4: GST structured data.

Covers (per the spec):
  - Pure-function GST invariant checks (app/domain/gst.py) across every
    required scenario: exclusive single GST, CGST+SGST pair, inclusive,
    item_level mixed 5%/18%, discount + GST together, CESS, no tax at all,
    and a deliberately inconsistent invoice.
  - The extraction-time adapter (app/extraction/validation.py:validate_gst)
    over the same scenarios, using ExtractedInvoice fixtures.
  - CHECK constraints on expenses.gst_mode, expense_tax_components (name,
    rate range, amount non-negative), expense_line_items.gst_rate/
    gst_amount_minor, and the UNIQUE(expense_id, name) constraint
    (@pytest.mark.postgres -- SQLite does not enforce CHECK).
  - The confirm-guard trigger on expense_tax_components, via both raw SQL
    and ORM session.commit() (@pytest.mark.postgres).
  - The gst_mode immutability extension to
    guard_expense_financial_immutability() (V3 -> V4).
  - Migration 0010's backfill, proven via a real upgrade -> downgrade ->
    upgrade round-trip against a legacy (pre-0010) expense.
  - The pipeline-guard interaction: a confirmed expense driven through
    _persist_pipeline_result must not get expense_tax_components rows,
    gst_mode, or needs_review mutated.
  - The confirm-endpoint 422 gate naming the specific failed invariant.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.domain.gst import check_gst_invariants
from app.domain.models import (
    Expense,
    ExpenseLineItem,
    ExpenseSource,
    ExpenseStatus,
    ExpenseTaxComponent,
    GstMode,
    LineItemKind,
    ParseStatus,
    TaxComponentName,
    User,
)
from app.extraction.schema import (
    ExtractedInvoice,
    ExtractedLineItem,
    ExtractedTaxComponent,
)
from app.extraction.validation import validate_gst

# ---------------------------------------------------------------------------
# Shared helpers (mirror tests/test_m6_vendor_discount_rules.py)
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, name: str, email: str | None = None) -> User:
    user = User(
        name=name, email=email or f"{name.lower()}_{uuid.uuid4().hex[:6]}@test.com"
    )
    db.add(user)
    await db.flush()
    return user


async def _make_expense(
    db: AsyncSession,
    payer: User,
    total: int = 100000,
    subtotal: int | None = None,
    gst_mode: GstMode = GstMode.none,
    needs_review: bool = False,
    parse_status: ParseStatus = ParseStatus.parsed,
) -> Expense:
    expense = Expense(
        paid_by=payer.id,
        vendor="Amazon",
        currency="INR",
        total_minor=total,
        subtotal_minor=subtotal if subtotal is not None else total,
        source=ExpenseSource.manual,
        parse_status=parse_status,
        status=ExpenseStatus.active,
        gst_mode=gst_mode,
        needs_review=needs_review,
    )
    db.add(expense)
    await db.flush()
    return expense


# ---------------------------------------------------------------------------
# 1. Pure-function GST invariant checks (app/domain/gst.py) — every scenario.
# ---------------------------------------------------------------------------


def test_exclusive_single_gst_reconciles() -> None:
    # 1000 item + 180 GST(18%) = 1180 total, no discount.
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[18000],
        invoice_total_minor=118000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert result.ok


def test_exclusive_cgst_sgst_pair_reconciles() -> None:
    # 1000 item + 9% CGST + 9% SGST = 1180 total.
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[9000, 9000],
        invoice_total_minor=118000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert result.ok


def test_inclusive_reconciles_and_components_informational() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_inclusive,
        item_totals_minor=118000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[18000],
        invoice_total_minor=118000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert result.ok


def test_inclusive_component_exceeding_total_is_flagged() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_inclusive,
        item_totals_minor=118000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[999999],
        invoice_total_minor=118000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert not result.ok
    assert result.issues[0].code == "gst_component_exceeds_total"


def test_item_level_mixed_rates_reconciles() -> None:
    # dish A: 100 @5% = 5; dish B: 200 @18% = 36; components: GST=41.
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=30000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[4100],
        invoice_total_minor=34100,
        line_gst_amounts_minor=[500, 3600],
        has_line_gst_data=True,
        has_component_data=True,
    )
    assert result.ok


def test_item_level_mismatch_flagged() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=30000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[9999],
        invoice_total_minor=34100,
        line_gst_amounts_minor=[500, 3600],
        has_line_gst_data=True,
        has_component_data=True,
    )
    assert not result.ok
    assert result.issues[0].code == "gst_item_level_mismatch"


def test_item_level_skipped_when_only_one_side_has_data() -> None:
    """Documented behaviour: if only line-item OR component data exists,
    there is nothing to reconcile against, so the check is skipped."""
    only_lines = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=30000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[],
        invoice_total_minor=34100,
        line_gst_amounts_minor=[500, 3600],
        has_line_gst_data=True,
        has_component_data=False,
    )
    assert only_lines.ok

    only_components = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=30000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[4100],
        invoice_total_minor=34100,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert only_components.ok


def test_discount_plus_gst_together_reconciles() -> None:
    # 1000 item - 100 discount + 162 GST(18% of 900) = 1062.
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000,
        discount_amount_minor=10000,
        tax_component_amounts_minor=[16200],
        invoice_total_minor=106200,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert result.ok


def test_cess_component_included_in_exclusive_sum() -> None:
    # 1000 item + 180 GST + 10 CESS = 1190.
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[18000, 1000],
        invoice_total_minor=119000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert result.ok


def test_no_tax_signal_at_all_is_ok() -> None:
    result = check_gst_invariants(
        gst_mode=GstMode.none,
        item_totals_minor=100000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[],
        invoice_total_minor=100000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=False,
    )
    assert result.ok


def test_refund_line_included_in_item_totals_reconciles() -> None:
    """
    M6 item 4 MEDIUM fix: a partial refund appends a signed, negative
    kind='refund' line (see app/api/expenses.py:create_refund). That line
    is intentionally INCLUDED (signed) in `item_totals_minor` -- see
    check_gst_invariants' docstring -- so refunded expenses still reconcile
    under every applicable gst_mode after the refund is applied.

    Scenario: an original 100000 item is partially refunded 20000 (net
    80000 remaining). Checked under each gst_mode that uses
    item_totals_minor.
    """
    # invoice_exclusive: (100000 item - 20000 refund) + 18000 GST == 98000.
    exclusive = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000 - 20000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[18000],
        invoice_total_minor=98000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert exclusive.ok

    # invoice_inclusive: (100000 item - 20000 refund) == 80000 total; the
    # GST component is informational only but must stay <= total.
    inclusive = check_gst_invariants(
        gst_mode=GstMode.invoice_inclusive,
        item_totals_minor=100000 - 20000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[14400],
        invoice_total_minor=80000,
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert inclusive.ok

    # item_level: the refund line carries no gst_amount_minor of its own
    # (refunds are recorded as a flat signed amount, not a per-line GST
    # breakdown), so it simply doesn't participate in the line_gst_amounts
    # side of this check -- the remaining item's own gst_amount_minor still
    # reconciles against the (unaffected) tax component sum.
    item_level = check_gst_invariants(
        gst_mode=GstMode.item_level,
        item_totals_minor=100000 - 20000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[5000],
        invoice_total_minor=98000,
        line_gst_amounts_minor=[5000],
        has_line_gst_data=True,
        has_component_data=True,
    )
    assert item_level.ok


def test_deliberately_inconsistent_invoice_flagged() -> None:
    """Numbers that cannot reconcile under any invariant."""
    result = check_gst_invariants(
        gst_mode=GstMode.invoice_exclusive,
        item_totals_minor=100000,
        discount_amount_minor=0,
        tax_component_amounts_minor=[18000],
        invoice_total_minor=999999,  # nowhere near 118000
        line_gst_amounts_minor=[],
        has_line_gst_data=False,
        has_component_data=True,
    )
    assert not result.ok
    assert result.issues[0].code == "gst_exclusive_mismatch"
    assert "999999" in result.detail()


# ---------------------------------------------------------------------------
# 2. Extraction-time adapter (validate_gst) over ExtractedInvoice fixtures.
# ---------------------------------------------------------------------------


def _line(
    line_no: int,
    kind: LineItemKind,
    total_minor: int,
    gst_rate: Decimal | None = None,
    gst_amount_minor: int | None = None,
) -> ExtractedLineItem:
    return ExtractedLineItem(
        line_no=line_no,
        kind=kind,
        total_minor=total_minor,
        unit_price_minor=total_minor,
        gst_rate=gst_rate,
        gst_amount_minor=gst_amount_minor,
    )


def test_validate_gst_exclusive_single_gst() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_exclusive,
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000)
        ],
        line_items=[_line(1, LineItemKind.item, 100000)],
        invoice_total_minor=118000,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_cgst_sgst_pair() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_exclusive,
        tax_components=[
            ExtractedTaxComponent(
                name=TaxComponentName.CGST, rate=Decimal("9.00"), amount_minor=9000
            ),
            ExtractedTaxComponent(
                name=TaxComponentName.SGST, rate=Decimal("9.00"), amount_minor=9000
            ),
        ],
        line_items=[_line(1, LineItemKind.item, 100000)],
        invoice_total_minor=118000,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_inclusive() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_inclusive,
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000)
        ],
        line_items=[_line(1, LineItemKind.item, 118000)],
        invoice_total_minor=118000,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_item_level_mixed() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.item_level,
        line_items=[
            _line(
                1,
                LineItemKind.item,
                10000,
                gst_rate=Decimal("5.00"),
                gst_amount_minor=500,
            ),
            _line(
                2,
                LineItemKind.item,
                20000,
                gst_rate=Decimal("18.00"),
                gst_amount_minor=3600,
            ),
        ],
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=4100),
        ],
        invoice_total_minor=34100,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_discount_plus_gst() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_exclusive,
        line_items=[
            _line(1, LineItemKind.item, 100000),
            _line(2, LineItemKind.discount, -10000),
        ],
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=16200)
        ],
        invoice_total_minor=106200,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_cess() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_exclusive,
        line_items=[_line(1, LineItemKind.item, 100000)],
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000),
            ExtractedTaxComponent(name=TaxComponentName.CESS, amount_minor=1000),
        ],
        invoice_total_minor=119000,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_no_tax_at_all() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.none,
        line_items=[_line(1, LineItemKind.item, 100000)],
        invoice_total_minor=100000,
    )
    assert validate_gst(invoice).ok


def test_validate_gst_deliberately_inconsistent_invoice() -> None:
    invoice = ExtractedInvoice(
        gst_mode=GstMode.invoice_exclusive,
        line_items=[_line(1, LineItemKind.item, 100000)],
        tax_components=[
            ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000)
        ],
        invoice_total_minor=999999,
    )
    result = validate_gst(invoice)
    assert not result.ok
    assert result.issues[0].code == "gst_exclusive_mismatch"


# ---------------------------------------------------------------------------
# 3. CHECK constraints (Postgres only).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_ck_expense_gst_mode_rejects_bogus(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET gst_mode = 'bogus' WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_ck_tax_component_invalid_name_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "INSERT INTO expense_tax_components "
                "(id, expense_id, name, rate, amount_minor) "
                "VALUES (:id, :expense_id, 'VAT', NULL, 100)"
            ),
            {"id": str(uuid.uuid4()), "expense_id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_ck_tax_component_rate_out_of_range_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "INSERT INTO expense_tax_components "
                "(id, expense_id, name, rate, amount_minor) "
                "VALUES (:id, :expense_id, 'GST', 150.00, 100)"
            ),
            {"id": str(uuid.uuid4()), "expense_id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_ck_tax_component_negative_amount_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "INSERT INTO expense_tax_components "
                "(id, expense_id, name, rate, amount_minor) "
                "VALUES (:id, :expense_id, 'GST', NULL, -1)"
            ),
            {"id": str(uuid.uuid4()), "expense_id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_uq_tax_component_expense_name_rejects_duplicate(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.CGST, amount_minor=9000
        )
    )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(
            ExpenseTaxComponent(
                expense_id=expense.id, name=TaxComponentName.CGST, amount_minor=1
            )
        )
        await db_session.commit()


@pytest.mark.postgres
async def test_ck_line_item_gst_rate_range_rejected(db_session: AsyncSession) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    li = ExpenseLineItem(
        expense_id=expense.id,
        line_no=1,
        kind=LineItemKind.item,
        quantity=1,
        total_minor=1000,
    )
    db_session.add(li)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expense_line_items SET gst_rate = 150.00 WHERE id = :id"),
            {"id": str(li.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_ck_line_item_gst_amount_nonneg_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    li = ExpenseLineItem(
        expense_id=expense.id,
        line_no=1,
        kind=LineItemKind.item,
        quantity=1,
        total_minor=1000,
    )
    db_session.add(li)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "UPDATE expense_line_items SET gst_amount_minor = -1 WHERE id = :id"
            ),
            {"id": str(li.id)},
        )
        await db_session.flush()


# ---------------------------------------------------------------------------
# 4. Confirm-guard trigger on expense_tax_components (Postgres only).
# ---------------------------------------------------------------------------


@pytest.mark.postgres
async def test_confirmed_expense_tax_component_insert_rejected_raw_sql(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, parse_status=ParseStatus.confirmed)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "INSERT INTO expense_tax_components "
                "(id, expense_id, name, rate, amount_minor) "
                "VALUES (:id, :expense_id, 'GST', NULL, 100)"
            ),
            {"id": str(uuid.uuid4()), "expense_id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_confirmed_expense_tax_component_insert_rejected_orm(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, parse_status=ParseStatus.confirmed)
    await db_session.commit()

    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=100
        )
    )
    with pytest.raises((DBAPIError, IntegrityError)):
        await db_session.commit()


@pytest.mark.postgres
async def test_confirmed_expense_tax_component_update_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    component = ExpenseTaxComponent(
        expense_id=expense.id, name=TaxComponentName.GST, amount_minor=100
    )
    db_session.add(component)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text(
                "UPDATE expense_tax_components SET amount_minor = 1 WHERE id = :id"
            ),
            {"id": str(component.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_confirmed_expense_gst_mode_mutation_rejected(
    db_session: AsyncSession,
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice, gst_mode=GstMode.invoice_exclusive)
    await db_session.commit()

    expense.parse_status = ParseStatus.confirmed
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            sa.text("UPDATE expenses SET gst_mode = 'none' WHERE id = :id"),
            {"id": str(expense.id)},
        )
        await db_session.flush()


@pytest.mark.postgres
async def test_draft_expense_tax_components_remain_mutable(
    db_session: AsyncSession,
) -> None:
    """Regression: the new guard must not over-block drafts."""
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(db_session, alice)
    await db_session.commit()

    await db_session.execute(
        sa.text(
            "INSERT INTO expense_tax_components "
            "(id, expense_id, name, rate, amount_minor) "
            "VALUES (:id, :expense_id, 'GST', NULL, 100)"
        ),
        {"id": str(uuid.uuid4()), "expense_id": str(expense.id)},
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# 5. Pipeline-guard interaction: confirmed expense survives the pipeline
#    persistence path untouched (mirrors test_m6_vendor_discount_rules.py's
#    test_persist_pipeline_result_confirmed_expense_survives_real_trigger).
# ---------------------------------------------------------------------------


async def test_persist_pipeline_result_never_writes_gst_for_confirmed_expense(
    db_session: AsyncSession,
) -> None:
    import app.extraction.tasks as tasks_module
    from app.extraction.pipeline import PipelineResult

    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(
        db_session, alice, parse_status=ParseStatus.confirmed, gst_mode=GstMode.none
    )
    await db_session.commit()

    pre_gst_mode = expense.gst_mode
    pre_needs_review = expense.needs_review

    pipeline_result = PipelineResult(
        parse_status=ParseStatus.parsed,
        raw_extraction={"vendor": "Amazon"},
        invoice=ExtractedInvoice(
            vendor="Amazon",
            invoice_total_minor=118000,
            subtotal_minor=100000,
            gst_mode=GstMode.invoice_exclusive,
            tax_components=[
                ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000)
            ],
            line_items=[_line(1, LineItemKind.item, 100000)],
        ),
        route="text",
    )

    await tasks_module._persist_pipeline_result(db_session, expense, pipeline_result)

    assert expense.gst_mode == pre_gst_mode == GstMode.none
    assert expense.needs_review == pre_needs_review is False

    components = (
        (
            await db_session.execute(
                sa.select(ExpenseTaxComponent).where(
                    ExpenseTaxComponent.expense_id == expense.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert components == []

    await db_session.commit()


@pytest.mark.postgres
async def test_persist_pipeline_result_confirmed_expense_survives_real_trigger_gst(
    db_session: AsyncSession,
) -> None:
    """Postgres-only: the real trg_tax_component_confirm_guard /
    guard_expense_financial_immutability V4 must not be hit at all, because
    the whole-function guard in _persist_pipeline_result never attempts the
    write in the first place."""
    import app.extraction.tasks as tasks_module
    from app.extraction.pipeline import PipelineResult

    alice = await _make_user(db_session, "Alice")
    expense = Expense(
        paid_by=alice.id,
        vendor="Amazon",
        currency="INR",
        total_minor=50000,
        subtotal_minor=50000,
        source=ExpenseSource.manual,
        parse_status=ParseStatus.confirmed,
        status=ExpenseStatus.active,
        gst_mode=GstMode.none,
        needs_review=False,
    )
    db_session.add(expense)
    await db_session.commit()

    pipeline_result = PipelineResult(
        parse_status=ParseStatus.parsed,
        raw_extraction={"vendor": "Amazon"},
        invoice=ExtractedInvoice(
            vendor="Amazon",
            invoice_total_minor=118000,
            subtotal_minor=100000,
            gst_mode=GstMode.invoice_exclusive,
            tax_components=[
                ExtractedTaxComponent(name=TaxComponentName.GST, amount_minor=18000)
            ],
            line_items=[_line(1, LineItemKind.item, 100000)],
        ),
        route="text",
    )

    await tasks_module._persist_pipeline_result(db_session, expense, pipeline_result)
    await db_session.commit()  # would raise via the real trigger if any write leaked
    await db_session.refresh(expense)

    assert expense.gst_mode == GstMode.none
    assert expense.needs_review is False
    assert expense.parse_status == ParseStatus.confirmed


# ---------------------------------------------------------------------------
# 6. Confirm-endpoint 422 gate naming the failed invariant.
# ---------------------------------------------------------------------------


async def test_confirm_rejected_when_needs_review(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(
        db_session,
        alice,
        total=999999,
        gst_mode=GstMode.invoice_exclusive,
        needs_review=True,
    )
    line = ExpenseLineItem(
        expense_id=expense.id,
        line_no=1,
        kind=LineItemKind.item,
        quantity=1,
        total_minor=100000,
    )
    db_session.add(line)
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=18000
        )
    )
    await db_session.commit()

    from app.config import settings
    from app.domain.auth import create_access_token

    token = create_access_token(alice.id, settings.SECRET_KEY)
    resp = await client.post(
        f"/api/v1/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "gst_exclusive_mismatch" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 7. CRITICAL fix (finance-logic-reviewer): correct_line_items must re-run
#    the full validator (base arithmetic AND GST) and re-derive
#    needs_review from the FRESH outcome; confirm_expense's GST check must
#    be unconditional, not gated on a possibly-stale needs_review flag.
# ---------------------------------------------------------------------------


def _token(user_id: uuid.UUID) -> str:
    from app.config import settings
    from app.domain.auth import create_access_token

    return create_access_token(user_id, settings.SECRET_KEY)


async def _needs_review_gst_expense(
    db_session: AsyncSession,
    alice: User,
    *,
    needs_review: bool,
    invoice_total_minor: int = 118000,
) -> Expense:
    """
    A `parse_status='needs_review'` expense (so PUT .../line-items is
    accepted) carrying `raw_extraction` in the shape
    `_last_known_invoice_total` expects, ready for a line-item correction.
    """
    expense = await _make_expense(
        db_session,
        alice,
        total=invoice_total_minor,
        subtotal=100000,
        gst_mode=GstMode.invoice_exclusive,
        needs_review=needs_review,
        parse_status=ParseStatus.needs_review,
    )
    expense.raw_extraction = {
        "attempts": [{"raw": {"invoice_total_minor": invoice_total_minor}}]
    }
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=18000
        )
    )
    await db_session.commit()
    return expense


async def test_correction_breaks_previously_clean_gst_reconciliation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    A correction whose new line items no longer reconcile against the
    UNTOUCHED existing expense_tax_components must flip needs_review from
    False -> True, and confirm must then be blocked naming the invariant --
    even though the correction's own base arithmetic (line items sum to the
    invoice total) is perfectly fine.
    """
    alice = await _make_user(db_session, "Alice")
    expense = await _needs_review_gst_expense(db_session, alice, needs_review=False)

    resp = await client.put(
        f"/api/v1/expenses/{expense.id}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item",
                    "total_minor": 90000,
                },
                {
                    "line_no": 2,
                    "kind": "tax",
                    "description": "Tax line",
                    "total_minor": 28000,
                },
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "parsed"
    assert body["needs_review"] is True

    confirm_resp = await client.post(
        f"/api/v1/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert confirm_resp.status_code == 422
    assert "gst_exclusive_mismatch" in confirm_resp.json()["detail"]


async def test_correction_repairs_previously_flagged_gst_needs_review(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    A correction whose new line items DO reconcile against the untouched
    tax components must clear a previously GST-caused needs_review=True
    back to False, and confirm must then succeed -- this is the endpoint's
    actual purpose for a GST-flagged expense.
    """
    alice = await _make_user(db_session, "Alice")
    expense = await _needs_review_gst_expense(db_session, alice, needs_review=True)

    resp = await client.put(
        f"/api/v1/expenses/{expense.id}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item",
                    "total_minor": 100000,
                },
                {
                    "line_no": 2,
                    "kind": "tax",
                    "description": "Tax line",
                    "total_minor": 18000,
                },
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "parsed"
    assert body["needs_review"] is False

    # Confirmable now: wire assignments then confirm.
    line_items = body["line_items"]
    assign_resp = await client.put(
        f"/api/v1/expenses/{expense.id}/assignments",
        json={
            "assignments": [
                {"line_item_id": li["id"], "user_id": str(alice.id)}
                for li in line_items
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert assign_resp.status_code == 200, assign_resp.text

    confirm_resp = await client.post(
        f"/api/v1/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["parse_status"] == "confirmed"


async def test_correction_stale_but_consistent_components_stays_clean(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    expense_tax_components is never rewritten by this endpoint (decision
    (ii)) -- a correction that reshapes the line items (adds a delivery fee
    row) but still nets to the same pre-tax subtotal the stale GST=18000
    component was computed against must stay needs_review=False.
    """
    alice = await _make_user(db_session, "Alice")
    expense = await _needs_review_gst_expense(db_session, alice, needs_review=False)

    resp = await client.put(
        f"/api/v1/expenses/{expense.id}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item",
                    "total_minor": 80000,
                },
                {
                    "line_no": 2,
                    "kind": "delivery_fee",
                    "description": "Delivery",
                    "total_minor": 20000,
                },
                {
                    "line_no": 3,
                    "kind": "tax",
                    "description": "Tax line",
                    "total_minor": 18000,
                },
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "parsed"
    assert body["needs_review"] is False


async def test_correction_stale_and_inconsistent_components_flags_needs_review(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    Same stale-components setup as above, but this time the corrected line
    items no longer net to what the untouched GST=18000 component was
    computed against -- must flag needs_review=True rather than going
    unchecked.
    """
    alice = await _make_user(db_session, "Alice")
    expense = await _needs_review_gst_expense(db_session, alice, needs_review=False)

    resp = await client.put(
        f"/api/v1/expenses/{expense.id}/line-items",
        json={
            "line_items": [
                {
                    "line_no": 1,
                    "kind": "item",
                    "description": "Item",
                    "total_minor": 50000,
                },
                {
                    "line_no": 2,
                    "kind": "delivery_fee",
                    "description": "Delivery",
                    "total_minor": 30000,
                },
                {
                    "line_no": 3,
                    "kind": "tax",
                    "description": "Tax line",
                    "total_minor": 38000,
                },
            ]
        },
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["parse_status"] == "parsed"
    # item_totals = 50000 + 30000 = 80000; + 18000 stale component = 98000,
    # but the corrected invoice total is still 118000 -> mismatch.
    assert body["needs_review"] is True


async def test_confirm_unconditional_check_catches_raw_sql_bad_state(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    Manufacture a bad state directly via raw SQL on a draft ('parsed')
    expense's expense_tax_components -- bypassing every app-level check,
    simulating a hypothetical future buggy write path that forgets to
    recompute needs_review. confirm_expense's GST check must catch this
    UNCONDITIONALLY (it no longer gates on expense.needs_review), even
    though needs_review was never set to True by any app code path.
    """
    alice = await _make_user(db_session, "Alice")
    expense = await _make_expense(
        db_session,
        alice,
        total=118000,
        subtotal=100000,
        gst_mode=GstMode.invoice_exclusive,
        needs_review=False,
        parse_status=ParseStatus.parsed,
    )
    db_session.add(
        ExpenseLineItem(
            expense_id=expense.id,
            line_no=1,
            kind=LineItemKind.item,
            quantity=1,
            total_minor=100000,
        )
    )
    db_session.add(
        ExpenseTaxComponent(
            expense_id=expense.id, name=TaxComponentName.GST, amount_minor=18000
        )
    )
    await db_session.commit()

    # Bypass the ORM object / application layer entirely -- a direct Core
    # UPDATE statement, exactly like a hypothetical future buggy write path
    # would issue, with no app-level validation or needs_review recompute
    # anywhere on this path.
    await db_session.execute(
        sa.update(ExpenseTaxComponent)
        .where(ExpenseTaxComponent.expense_id == expense.id)
        .values(amount_minor=999999)
    )
    await db_session.commit()
    await db_session.refresh(expense)
    assert expense.needs_review is False  # never touched by the raw SQL above

    resp = await client.post(
        f"/api/v1/expenses/{expense.id}/confirm",
        headers={"Authorization": f"Bearer {_token(alice.id)}"},
    )
    assert resp.status_code == 422
    assert "gst_exclusive_mismatch" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 8. Migration 0010 backfill, proven via a real upgrade/downgrade/upgrade
#    round-trip against a legacy (pre-0010) expense.
# ---------------------------------------------------------------------------

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)


def _run_alembic(*args: str, database_url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


@pytest.mark.postgres
async def test_migration_0010_backfill_round_trip(
    request: pytest.FixtureRequest,
) -> None:
    """
    Real alembic upgrade -> downgrade -> upgrade cycle (invoked as a
    subprocess so app.config.settings.DATABASE_URL, which is read once at
    import time, always matches TEST_DATABASE_URL for that subprocess) --
    proves migration 0010's backfill is correct AND idempotent across a
    round-trip, using a legacy (pre-0010) expense with an amount-based
    kind='tax' line item.
    """
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    if "postgresql" not in test_url:
        pytest.skip("requires TEST_DATABASE_URL to be a Postgres URL")

    eng = create_async_engine(test_url, echo=False)
    try:
        async with eng.begin() as conn:
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))

        # Build schema up to (but not including) migration 0010.
        _run_alembic("upgrade", "0009", database_url=test_url)

        user_id = uuid.uuid4()
        expense_id = uuid.uuid4()
        line_id = uuid.uuid4()

        async with eng.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO users (id, name, email, default_currency, created_at) "
                    "VALUES (:id, 'Alice', :email, 'INR', now())"
                ),
                {"id": str(user_id), "email": f"alice_{user_id.hex[:6]}@test.com"},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO expenses "
                    "(id, paid_by, vendor, currency, subtotal_minor, total_minor, "
                    "source, parse_status, status, created_at) "
                    "VALUES (:id, :paid_by, 'Amazon', 'INR', 100000, 118000, "
                    "'manual', 'parsed', 'active', now())"
                ),
                {"id": str(expense_id), "paid_by": str(user_id)},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO expense_line_items "
                    "(id, expense_id, line_no, kind, quantity, total_minor) "
                    "VALUES (:id, :expense_id, 1, 'tax', 1, 18000)"
                ),
                {"id": str(line_id), "expense_id": str(expense_id)},
            )

        pre_snapshot: dict[str, object] = {}
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT total_minor, subtotal_minor FROM expenses WHERE id = :id"
                    ),
                    {"id": str(expense_id)},
                )
            ).one()
            pre_snapshot = {"total_minor": row[0], "subtotal_minor": row[1]}

        # Apply 0010 -- this is where the backfill runs.
        _run_alembic("upgrade", "head", database_url=test_url)

        async with eng.connect() as conn:
            components = (
                await conn.execute(
                    sa.text(
                        "SELECT name, rate, amount_minor FROM expense_tax_components "
                        "WHERE expense_id = :id"
                    ),
                    {"id": str(expense_id)},
                )
            ).all()
            gst_mode_row = (
                await conn.execute(
                    sa.text("SELECT gst_mode FROM expenses WHERE id = :id"),
                    {"id": str(expense_id)},
                )
            ).one()
            post_row = (
                await conn.execute(
                    sa.text(
                        "SELECT total_minor, subtotal_minor FROM expenses WHERE id = :id"
                    ),
                    {"id": str(expense_id)},
                )
            ).one()

        assert len(components) == 1
        name, rate, amount_minor = components[0]
        assert name == "GST"
        assert rate is None
        assert amount_minor == 18000
        assert gst_mode_row[0] == "invoice_exclusive"

        # Ledger/balance-affecting figures are byte-identical before/after.
        assert {
            "total_minor": post_row[0],
            "subtotal_minor": post_row[1],
        } == pre_snapshot

        # The backfilled component reconciles cleanly under the exclusive
        # invariant: item(s) other than the tax line sum to 100000, plus
        # the 18000 GST component, equals the 118000 total.
        check = check_gst_invariants(
            gst_mode=GstMode.invoice_exclusive,
            item_totals_minor=post_row[1],  # subtotal_minor == non-tax items
            discount_amount_minor=0,
            tax_component_amounts_minor=[amount_minor],
            invoice_total_minor=post_row[0],
            line_gst_amounts_minor=[],
            has_line_gst_data=False,
            has_component_data=True,
        )
        assert check.ok

        # Round-trip: downgrade then upgrade again must not error and must
        # not double-insert (the table is dropped and recreated by
        # downgrade/upgrade, so there is nothing left to collide with the
        # UNIQUE(expense_id, name) constraint).
        _run_alembic("downgrade", "-1", database_url=test_url)
        _run_alembic("upgrade", "head", database_url=test_url)

        async with eng.connect() as conn:
            components_again = (
                await conn.execute(
                    sa.text(
                        "SELECT name, amount_minor FROM expense_tax_components "
                        "WHERE expense_id = :id"
                    ),
                    {"id": str(expense_id)},
                )
            ).all()
        assert len(components_again) == 1
        assert components_again[0] == ("GST", 18000)
    finally:
        async with eng.begin() as conn:
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))
        await eng.dispose()
