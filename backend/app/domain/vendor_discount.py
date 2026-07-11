"""
M6 item 3: vendor discount rule matching.

`match_rule` is a PURE function (no I/O, no DB session) that picks the best
applicable rule for a given already-normalized vendor string, subtotal, and
group -- from a list of already-loaded `VendorDiscountRule` ORM objects. The
thin async DB-query wrapper `find_matching_rule` lives at the bottom of this
module and does the loading, then delegates to `match_rule`.

Vendor matching strategy
-------------------------
`vendor_normalized` is compared to `rule.vendor_pattern` via EXACT STRING
EQUALITY ONLY (both sides pre-normalized via
`app.extraction.vendor_detect.normalize_vendor_text`). We deliberately do
NOT use substring containment: vendor names reaching this code have already
been canonicalized by `app.extraction.vendor_detect.resolve_vendor()` (e.g.
"Amazon", "Swiggy", "Zomato", ...), so an exact match is both sufficient and
unambiguous. Substring matching would risk false positives -- e.g. a rule
for "amazon" would also match a hypothetical future vendor whose canonical
name happens to contain "amazon" as a substring, or a rule for a short
generic token would spuriously match many vendors. Exact equality is the
conservative, deterministic choice and matches vendor_detect.py's own
"never guess wrong" philosophy for vendor identification.

Scope precedence
-----------------
Group-scoped rules (rule.group_id == the expense's group_id) beat ALL
global rules (rule.group_id is None) outright, regardless of discount size.
This is a deliberate business decision: a group's own configured rule is
assumed more relevant/intentional to that group's members than any of the
acting user's personal global rules, so scopes are never compared against
each other by discount magnitude -- if any group rule applies, no global
rule is even considered.

Threshold + amount computation
--------------------------------
Among same-scope, active rules: a rule applies iff
`subtotal_minor >= rule.min_order_total_minor` (inclusive -- exactly equal
qualifies). For each applicable rule the discount amount is:
  - flat:    rule.discount_value_minor, capped at subtotal_minor (a
             discount can never exceed the subtotal it's discounting).
  - percent: round(subtotal_minor * discount_percent / 100), using
             round-half-even (Python's builtsin `round()` on a Decimal,
             matching the "same rounding convention used elsewhere" --
             see app/domain/rounding.py's docstring: this is a SINGLE-value
             rounding, not a multi-way remainder split, so
             allocate_largest_remainder does not apply here; round-half-even
             is the simplest, standard, deterministic choice), also capped
             at subtotal_minor.

The rule with the LARGEST computed discount amount wins among the winning
scope's applicable rules. Ties are broken deterministically by rule id,
lowest UUID wins (UUIDs are opaque but total-ordered; picking "lowest" is
an arbitrary-but-stable, reproducible tie-break -- any fixed total order
would do, this is the one implemented).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DiscountSource,
    DiscountType,
    Expense,
    ParseStatus,
    VendorDiscountRule,
)
from app.domain.rounding import percent_of_minor
from app.extraction.vendor_detect import normalize_vendor_text

if TYPE_CHECKING:
    from app.extraction.schema import ExtractedInvoice


def compute_discount_amount(rule: VendorDiscountRule, subtotal_minor: int) -> int:
    """
    Compute the minor-unit discount amount a single rule would produce
    against `subtotal_minor`, capped so it never exceeds the subtotal.
    Does NOT check min_order_total_minor -- callers must gate on that
    separately (see `match_rule`).
    """
    if rule.discount_type == DiscountType.flat:
        amount = int(rule.discount_value_minor or 0)
    else:
        percent = Decimal(str(rule.discount_percent or 0))
        # Shared single-value rounding rule -- see app/domain/rounding.py's
        # percent_of_minor docstring. Also used by app/domain/splitting.py's
        # compute_allocation for percent-based expense discounts, so both
        # call sites round identically.
        amount = percent_of_minor(subtotal_minor, percent)
    return max(0, min(amount, subtotal_minor))


def match_rule(
    vendor_normalized: str,
    subtotal_minor: int,
    group_id: uuid.UUID | None,
    rules: list[VendorDiscountRule],
) -> VendorDiscountRule | None:
    """
    Pick the best applicable, active VendorDiscountRule for this vendor /
    subtotal / group, or None if no rule applies. Pure function -- `rules`
    must already be loaded (active rules scoped to `group_id` and/or
    global). See module docstring for the full precedence rules.
    """
    candidates = [
        r for r in rules if r.active and r.vendor_pattern == vendor_normalized
    ]
    if not candidates:
        return None

    group_scoped = (
        [r for r in candidates if group_id is not None and r.group_id == group_id]
        if group_id is not None
        else []
    )
    scoped_pool = (
        group_scoped if group_scoped else [r for r in candidates if r.group_id is None]
    )

    applicable = [
        r for r in scoped_pool if subtotal_minor >= int(r.min_order_total_minor)
    ]
    if not applicable:
        return None

    def _sort_key(r: VendorDiscountRule) -> tuple[int, str]:
        amount = compute_discount_amount(r, subtotal_minor)
        # Larger amount wins -> sort descending by amount, then ascending by
        # id (lowest UUID) as the deterministic tie-break.
        return (-amount, str(r.id))

    applicable.sort(key=_sort_key)
    return applicable[0]


async def find_matching_rule(
    db: AsyncSession,
    vendor_normalized: str,
    subtotal_minor: int,
    group_id: uuid.UUID | None,
) -> VendorDiscountRule | None:
    """
    Thin DB-query wrapper around `match_rule`: loads every active candidate
    rule (group-scoped rules for `group_id`, UNIONed with global rules
    where group_id IS NULL) and delegates the actual matching decision to
    the pure function above.
    """
    conditions = [VendorDiscountRule.group_id.is_(None)]
    if group_id is not None:
        conditions.append(VendorDiscountRule.group_id == group_id)

    stmt = select(VendorDiscountRule).where(
        VendorDiscountRule.active.is_(True),
        VendorDiscountRule.vendor_pattern == vendor_normalized,
        or_(*conditions),
    )
    result = await db.execute(stmt)
    rules = list(result.scalars().all())
    return match_rule(vendor_normalized, subtotal_minor, group_id, rules)


async def apply_vendor_discount_snapshot(
    db: AsyncSession,
    expense: Expense,
    *,
    subtotal_override_minor: int | None = None,
) -> None:
    """
    Auto-application hook (M6 item 3, spec section 5): mutates `expense`'s
    discount_* snapshot columns in place if a vendor rule matches. Does NOT
    commit -- caller is responsible for the surrounding transaction (both
    call sites, app/extraction/tasks.py and app/api/expenses.py, already
    commit as part of their own flow).

    M6 item 5 (OQ-2 fix): the threshold check inside `match_rule` must be
    compared against the same "fresh-computed base subtotal" (items + fees
    + tip + refunds, excluding tax/discount lines -- see
    app.domain.gst.is_base_gst_line / base_item_totals_minor, the single
    shared definition also used by app.domain.splitting.compute_allocation
    at allocation time) that item 5's allocator uses for its own threshold
    check, so a rule that "applies" here can never silently fail to apply
    (or vice versa) at confirm time purely because the two call sites used
    two different notions of "subtotal". `subtotal_override_minor`, when
    given, is used INSTEAD of `expense.subtotal_minor` for both the
    not-yet-known-anything-to-match-against gate and the threshold
    comparison passed to `find_matching_rule`.

    app/extraction/tasks.py (the PDF pipeline call site) passes the
    fresh-computed subtotal of the in-memory `ExtractedInvoice.line_items`
    it is about to persist (the freshest, most accurate source available at
    that point in the pipeline -- the DB's expense_line_items rows are still
    the OLD, about-to-be-replaced set). app/api/expenses.py's manual-expense
    call site has no such override available (there is no separate
    "invoice" object; the persisted line items ARE the source of truth by
    the time this runs) and continues to fall back to
    `expense.subtotal_minor`, unchanged from before this fix.

    IMPORTANT -- informational snapshot only, does not itself post money:
    this function only writes the discount_* SNAPSHOT columns. Whether
    those columns actually change what anyone owes is entirely up to the
    caller: as of M6 item 5, `app.domain.splitting.compute_allocation` DOES
    read this snapshot (via `discount_spec_from_expense`) when an expense is
    confirmed -- see app/api/expenses.py:confirm_expense. This function
    itself still does no ledger/share math; it only produces the snapshot
    that a later, separate step may or may not consume.

    Gating (never even attempts a write path against a confirmed expense --
    the DB trigger from migration 0009 is only a backstop, not the primary
    defense):
      - No-op if expense.parse_status == 'confirmed'.
      - No-op if expense.discount_source == 'manual' (manual always wins;
        never auto-overwritten).
      - No-op if expense.vendor or the effective subtotal (override or
        expense.subtotal_minor) is unset (nothing to match against yet).

    Precedence when a rule DOES match: vendor_rule overwrites a prior
    'extracted' snapshot (more specific/intentional; see module docstring
    reference in the calling sites) as well as a prior 'vendor_rule'
    snapshot (re-evaluation on re-extraction, per spec section 5 -- "never
    touch manual" is the only carve-out). If no rule matches, any existing
    'extracted' or 'vendor_rule' snapshot is left as-is (this function only
    ever adds/overwrites a match; it never clears a snapshot that came from
    elsewhere -- absence of a vendor rule is not evidence the invoice has no
    discount).
    """
    if expense.parse_status == ParseStatus.confirmed:
        return
    if expense.discount_source == DiscountSource.manual:
        return
    effective_subtotal = (
        subtotal_override_minor
        if subtotal_override_minor is not None
        else expense.subtotal_minor
    )
    if not expense.vendor or effective_subtotal is None:
        return

    vendor_normalized = normalize_vendor_text(str(expense.vendor))
    group_id = expense.group_id
    rule = await find_matching_rule(
        db, vendor_normalized, int(effective_subtotal), group_id
    )
    if rule is None:
        return

    expense.discount_source = DiscountSource.vendor_rule
    expense.discount_rule_id = rule.id
    expense.discount_type = rule.discount_type
    expense.discount_value_minor = rule.discount_value_minor
    expense.discount_percent = rule.discount_percent
    expense.discount_threshold_minor = rule.min_order_total_minor


def apply_extracted_discount_snapshot(
    expense: Expense, invoice: ExtractedInvoice
) -> None:
    """
    M6 item 4 (discount follow-up to item 3): populate expense's discount_*
    snapshot columns directly from a FRESHLY extracted, printed coupon/
    promo line (`invoice.discount`, an `ExtractedDiscount` -- see
    app/extraction/schema.py), at draft-creation/re-extraction time.

    Pure mutation, no I/O, no commit (matches apply_vendor_discount_snapshot's
    contract -- the caller, app/extraction/tasks.py, commits as part of its
    own transaction, and only calls this from INSIDE the same
    original_status-confirmed guard that protects every other mutation in
    that function).

    Precedence (same "never overwrite manual" rule item 3 established, see
    apply_vendor_discount_snapshot's docstring):
      - No-op if expense.discount_source == 'manual'.
      - No-op if the invoice has no structured `discount` block at all.
      - Otherwise sets discount_source='extracted' and overwrites any prior
        'extracted' or 'vendor_rule' snapshot. This function is always
        called BEFORE apply_vendor_discount_snapshot in
        app/extraction/tasks.py, so if a vendor rule ALSO matches on this
        same run, its 'vendor_rule' snapshot correctly wins and overwrites
        whatever this function just wrote -- 'vendor_rule' is treated as
        more specific/intentional than a bare printed line, exactly as it
        already is relative to a historical 'extracted' backfill (migration
        0009).
    """
    if expense.discount_source == DiscountSource.manual:
        return
    discount = invoice.discount
    if discount is None:
        return

    expense.discount_source = DiscountSource.extracted
    expense.discount_type = discount.type
    expense.discount_value_minor = discount.value_minor
    expense.discount_percent = discount.percent
    expense.discount_threshold_minor = discount.threshold_minor
    expense.discount_rule_id = None
