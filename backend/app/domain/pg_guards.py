"""
PostgreSQL DDL strings for the append-only / financial-immutability guards.

Extracted here so both migration 0002 and the test conftest can reuse the
same strings without circular imports or numeric-prefixed module hackery.
"""

from __future__ import annotations

LEDGER_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION guard_ledger_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'ledger_entries is append-only: DELETE is not permitted (id=%). '
            'Create a new adjustment entry instead.',
            OLD.id;
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'ledger_entries is append-only: UPDATE is not permitted (id=%). '
            'Create a new adjustment entry instead.',
            OLD.id;
    END IF;
    RETURN NULL;
END;
$$;
"""

LEDGER_TRIGGER_DDL = """
CREATE TRIGGER trg_ledger_append_only
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION guard_ledger_append_only();
"""

EXPENSE_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id are immutable once confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

EXPENSE_TRIGGER_DDL = """
CREATE TRIGGER trg_expense_immutability
BEFORE UPDATE OR DELETE ON expenses
FOR EACH ROW EXECUTE FUNCTION guard_expense_financial_immutability();
"""


# ---------------------------------------------------------------------------
# M6 item 1: generic "reject mutation once parent expense is confirmed" guard
# ---------------------------------------------------------------------------
#
# One reusable trigger FUNCTION, parameterized via trigger arguments
# (TG_ARGV) so it can be attached to any child table that needs the same
# "no mutation once the parent expense is confirmed" rule:
#   - item_assignments (this item): joins via line_item_id ->
#     expense_line_items.expense_id (an indirect FK path).
#   - expense_tax_components (planned, M6 item 4): direct expense_id FK.
#   - Future direct-FK tables can reuse this same function unmodified.
#
# TG_ARGV[0] = name of the FK column on the trigging row to inspect
#              (e.g. 'line_item_id' or 'expense_id').
# TG_ARGV[1] = join mode: 'direct'        -> TG_ARGV[0] IS the expense_id
#                          'via_line_item' -> TG_ARGV[0] is a line_item_id;
#                                             resolve via expense_line_items.
#
# Same-transaction escape hatch
# ------------------------------
# The expense confirm flow (POST /expenses/{id}/confirm) legitimately
# mutates item_assignments (freezing share_minor, inserting audit rows for
# unassigned allocation) in the SAME transaction that flips
# expenses.parse_status to 'confirmed' -- and does so via
# post_expense_to_ledger() *before* those assignment writes are issued.
# A naive "block if parent status = confirmed" check would therefore reject
# the confirm flow's own bookkeeping writes.
#
# We distinguish "already confirmed in a prior, committed transaction"
# (must block) from "being confirmed right now, in this same in-flight
# transaction" (must allow) by comparing the expense row's xmin (the id of
# the transaction that last wrote it) against the current transaction id.
# If they match, the expense row was written by *this* transaction, so any
# further mutation of its children within the same transaction is the
# confirm flow finishing its own atomic write -- not a stale client trying
# to mutate a row it can already observe as confirmed.
#
# *** TRUST BOUNDARY, read before adding code to the confirm transaction ***
# The same-transaction escape hatch means this guard grants BLANKET
# permission to mutate item_assignments to ANY code that runs inside the
# same DB transaction as the statement that sets expenses.parse_status =
# 'confirmed' -- not just the specific freeze-shares code that exists
# today. The trigger cannot distinguish "the confirm flow's own,
# reviewed bookkeeping" from "some other bug/feature that happens to run
# in that transaction" -- both see the expense as writable-by-me and
# proceed unguarded. This is intentional (it is how confirm finalizes its
# own children) but it means: the confirm transaction is NOT a place this
# guard protects you from yourself. Any future code added to
# POST /expenses/{id}/confirm's transaction (or anything else that writes
# expenses.parse_status = 'confirmed' and then touches item_assignments
# before committing) can freely INSERT/UPDATE/DELETE item_assignments rows
# with NO trigger protection whatsoever. Do not assume this trigger catches
# bugs introduced inside that transaction -- it only catches mutation
# attempts from OUTSIDE it (a different, later transaction against an
# already-committed confirmed expense).
#
# Second escape hatch -- append-only refund audit rows
# ------------------------------------------------------
# Discovered empirically while running the FULL existing suite against this
# guard (tests/test_api_m2.py refund tests): POST /expenses/{id}/refunds
# legitimately INSERTs brand-new item_assignments rows (negative frozen
# shares) on an ALREADY-confirmed expense, in a transaction separate from
# the original confirm -- this is intentional, pre-existing, tested
# behaviour (a refund is itself an appended correction, per the ledger's
# own "corrections are new signed entries" convention in CLAUDE.md). Those
# rows always attach to a freshly-created ExpenseLineItem of
# kind='refund'. We therefore allow INSERT (never UPDATE/DELETE) when the
# child row's line item is a refund line -- i.e. item_assignments behaves
# like the ledger itself once its expense is confirmed: append-only, not
# fully frozen. This exception only applies to the 'via_line_item' join
# mode (it is meaningless for a table with no line-item concept).
GENERIC_CONFIRM_GUARD_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION reject_mutation_if_expense_confirmed()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    fk_column     text := TG_ARGV[0];
    join_mode     text := TG_ARGV[1];
    row_data      jsonb;
    fk_value      uuid;
    v_expense_id  uuid;
    v_parse_status text;
    v_expense_xmin xid;
    v_is_refund_line boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        row_data := to_jsonb(NEW);
    ELSE
        -- UPDATE and DELETE both guard against mutating/removing a row that
        -- already belongs to a confirmed expense (checked via OLD).
        row_data := to_jsonb(OLD);
    END IF;

    fk_value := (row_data ->> fk_column)::uuid;
    IF fk_value IS NULL THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    IF join_mode = 'direct' THEN
        v_expense_id := fk_value;
    ELSIF join_mode = 'via_line_item' THEN
        SELECT expense_id, (kind = 'refund')
        INTO v_expense_id, v_is_refund_line
        FROM expense_line_items
        WHERE id = fk_value;
    ELSE
        RAISE EXCEPTION
            'reject_mutation_if_expense_confirmed: unknown join_mode % '
            '(expected ''direct'' or ''via_line_item'')', join_mode;
    END IF;

    IF v_expense_id IS NULL THEN
        -- No resolvable parent (orphan / FK not yet enforced) -- let normal
        -- FK constraints handle referential integrity; nothing to guard.
        RETURN COALESCE(NEW, OLD);
    END IF;

    SELECT parse_status, xmin INTO v_parse_status, v_expense_xmin
    FROM expenses
    WHERE id = v_expense_id;

    IF v_parse_status = 'confirmed' THEN
        -- Escape hatch 1: the expense row itself was last written by the
        -- CURRENT transaction (the confirm flow freezing its own children
        -- before commit).
        IF v_expense_xmin::text::bigint =
           (pg_current_xact_id()::text::bigint % 4294967296) THEN
            RETURN COALESCE(NEW, OLD);
        END IF;

        -- Escape hatch 2: appending (INSERT only) a fresh audit row onto a
        -- refund line -- the append-only refund-correction pattern.
        IF TG_OP = 'INSERT' AND join_mode = 'via_line_item'
           AND v_is_refund_line THEN
            RETURN NEW;
        END IF;

        RAISE EXCEPTION
            'Cannot % row in %: parent expense (id=%) is confirmed and '
            'its children are immutable. Post a new correction instead.',
            TG_OP, TG_TABLE_NAME, v_expense_id;
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$;
"""

ITEM_ASSIGNMENT_CONFIRM_GUARD_TRIGGER_DDL = """
CREATE TRIGGER trg_item_assignment_confirm_guard
BEFORE INSERT OR UPDATE OR DELETE ON item_assignments
FOR EACH ROW EXECUTE FUNCTION reject_mutation_if_expense_confirmed(
    'line_item_id', 'via_line_item'
);
"""

# ---------------------------------------------------------------------------
# M6 item 1 (folded-in re-audit gap): expenses.parse_status state machine
# ---------------------------------------------------------------------------
#
# Re-audit finding: guard_expense_financial_immutability (migration 0002)
# blocked mutating specific *financial* columns of a confirmed expense, but
# never guarded the parse_status column itself -- a raw
# `UPDATE expenses SET parse_status = 'parsed' WHERE id = ...` on an
# already-confirmed expense used to succeed silently. Folded into the SAME
# trigger function (guard_expense_financial_immutability) rather than a new
# trigger, per the existing 0002 pattern: this is a superset CREATE OR
# REPLACE of that function's body, applied by migration 0006. The trigger
# object itself (trg_expense_immutability, created in 0002) is untouched --
# replacing the function it points to is enough for it to pick up the new
# behaviour.
#
# --- Legal transition graph (derived from actual code, not guessed) ---
# Grepped every assignment to `expense.parse_status` /
# `Expense(parse_status=...)` across app/ (see migration 0006's docstring
# for the full file-by-file trace). The transitions actually exercised by
# real code paths are exactly:
#
#   queued        -> parsed         (app/extraction/tasks.py: pipeline
#                                     validation passes)
#   queued        -> needs_review   (app/extraction/tasks.py: pipeline
#                                     validation fails / provider down)
#   needs_review  -> parsed         (PUT .../line-items correction endpoint,
#                                     app/api/expenses.py -- gated there on
#                                     OLD.parse_status == 'needs_review')
#   parsed        -> confirmed      (POST .../confirm, via
#                                     post_expense_to_ledger())
#
# NOT found anywhere in current application code, therefore NOT included
# as legal below (flagged back rather than guessed, per instructions):
#   - any transition INTO 'failed' -- the enum value and CHECK constraint
#     both allow it (migration 0001) but no code path ever sets it. It
#     remains reachable only via direct INSERT (row creation, which this
#     trigger does not restrict -- only UPDATE transitions are validated;
#     a fresh row simply starts in whatever state it starts in).
#   - 'queued' -> 'confirmed' directly (skipping 'parsed') -- manual
#     (non-upload) expense creation *starts* a brand-new row already at
#     'parsed' (an INSERT, not a transition -- see
#     app/api/expenses.py POST /expenses, parse_status=ParseStatus.parsed
#     set at construction time), it never transitions an existing 'queued'
#     row straight to 'confirmed'. If a future manual-entry path needs a
#     genuine queued->confirmed transition, add it explicitly here when
#     that code exists -- do not assume this trigger already permits it.
#
# Confirmed is TERMINAL: unlike the item_assignments guard above, there is
# NO same-transaction escape hatch for this check. The confirm flow only
# ever needs to WRITE 'confirmed' once (parsed -> confirmed); it never
# needs to re-write or revert it, even within its own transaction, so
# there is no legitimate case to protect. Any attempt to change
# parse_status away from 'confirmed' is rejected unconditionally, from any
# transaction, including the one that just set it.
EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.parse_status IS DISTINCT FROM OLD.parse_status THEN
            IF OLD.parse_status = 'confirmed' THEN
                -- Terminal state: no same-transaction escape hatch here
                -- (see comment above this DDL for why none is needed).
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> % -- confirmed is terminal and can never change.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            ELSIF NOT (
                (OLD.parse_status = 'queued' AND
                 NEW.parse_status IN ('parsed', 'needs_review'))
                OR (OLD.parse_status = 'needs_review' AND
                    NEW.parse_status = 'parsed')
                OR (OLD.parse_status = 'parsed' AND
                    NEW.parse_status = 'confirmed')
            ) THEN
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> %. Legal transitions: queued->parsed, '
                    'queued->needs_review, needs_review->parsed, '
                    'parsed->confirmed.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            END IF;
        END IF;

        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id are immutable once confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

# ---------------------------------------------------------------------------
# M6 item 1 addendum: 'failed' is a reserved, not-yet-wired state
# ---------------------------------------------------------------------------
#
# Re-audit follow-up: EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL above (applied
# by migration 0006) has NO legal transition into or out of 'failed'. Since
# it is reachable via direct INSERT (nothing restricts a row's initial
# value), a row inserted as 'failed' could never leave that state under
# 0006's trigger -- confirmed by inspection of the transition list: OLD.
# parse_status = 'failed' matches none of the three ELSIF branches, so any
# UPDATE attempting a transition away from 'failed' is unconditionally
# rejected as "not a legal transition."
#
# docs/ARCHITECTURE.md documents 'failed' as real, intended product
# behaviour, NOT dead code to delete:
#   - §"Corrupted/unsupported PDF — manual fallback": parse_status='failed'
#     opens the Quick Manual Entry flow.
#   - §pipeline rationale, point 4: "failed parses can be replayed against
#     improved prompts/models later without asking users to re-upload" --
#     i.e. a failed extraction is expected to be retried, not stuck.
#
# Neither the Quick Manual Entry flow nor a retry/replay endpoint exists in
# app/ yet (same "not yet wired" status as 'failed' itself), so this is
# RESERVED for that future work, not something exercised by code today.
# We nonetheless declare the transitions now so the trigger's legal-list
# matches documented product intent instead of silently omitting a state
# the schema has allowed since migration 0001:
#   queued -> failed   (pipeline: PDF is corrupted/unsupported, extraction
#                        cannot even attempt validation -- distinct from
#                        needs_review, which implies a partial attempt)
#   failed -> queued   (retry/replay against improved prompts/models, per
#                        the ARCHITECTURE.md point 4 rationale above)
#
# Deliberately NOT added: failed -> parsed (the Quick Manual Entry flow's
# landing state) -- plausible from the architecture doc, but nothing asked
# for it explicitly and no code path is even sketched for it; adding it now
# would be guessing beyond the reserved queued<->failed pair above. Add it
# explicitly, with its own justification, when that flow is actually built.
EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2 = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.parse_status IS DISTINCT FROM OLD.parse_status THEN
            IF OLD.parse_status = 'confirmed' THEN
                -- Terminal state: no same-transaction escape hatch here
                -- (see comment above EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL
                -- for why none is needed).
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> % -- confirmed is terminal and can never change.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            ELSIF NOT (
                (OLD.parse_status = 'queued' AND
                 NEW.parse_status IN ('parsed', 'needs_review', 'failed'))
                OR (OLD.parse_status = 'needs_review' AND
                    NEW.parse_status = 'parsed')
                OR (OLD.parse_status = 'parsed' AND
                    NEW.parse_status = 'confirmed')
                OR (OLD.parse_status = 'failed' AND
                    NEW.parse_status = 'queued')
            ) THEN
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> %. Legal transitions: queued->parsed, '
                    'queued->needs_review, queued->failed, '
                    'needs_review->parsed, parsed->confirmed, '
                    'failed->queued.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            END IF;
        END IF;

        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id are immutable once confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

# ---------------------------------------------------------------------------
# M6 item 3: extend guard_expense_financial_immutability() to also guard the
# new discount_* snapshot columns (migration 0009) on the expenses table
# itself.
# ---------------------------------------------------------------------------
#
# Why extend THIS function rather than reuse reject_mutation_if_expense_
# confirmed() (the generic child-table guard from migration 0006/M6 item 1):
# that function's whole design is built around resolving a CHILD row's
# parent expense via an FK column (direct expense_id, or via_line_item ->
# expense_line_items.expense_id) and then checking THAT expense's
# parse_status. The new discount_* columns live directly ON the expenses
# row -- there is no child table and no FK-join to resolve; OLD/NEW are
# already the expense row itself. Bolting a "join_mode = self" branch onto
# reject_mutation_if_expense_confirmed() would be a worse fit than simply
# adding these columns to the existing list of financial columns already
# guarded in guard_expense_financial_immutability() (total_minor,
# subtotal_minor, paid_by, currency, group_id) -- same shape of check
# (OLD.parse_status = 'confirmed' AND NEW.<col> IS DISTINCT FROM OLD.<col>),
# same trigger object (trg_expense_immutability, created once in migration
# 0002, never re-created), just a wider column list. This mirrors exactly
# how EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V2 itself was produced from
# V1 in migration 0007 -- a CREATE OR REPLACE widening the same function's
# body, not a new function or new trigger.
#
# discount_rule_id is included in the immutable-once-confirmed list even
# though it's an FK, not raw money, because it is part of the same
# snapshot: once an expense is confirmed, NEITHER its discount_type/value/
# percent/threshold NOR the rule id that produced them may be changed --
# they are audit lineage frozen at confirmation time, exactly like
# item_assignments.share_minor.
EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V3 = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.parse_status IS DISTINCT FROM OLD.parse_status THEN
            IF OLD.parse_status = 'confirmed' THEN
                -- Terminal state: no same-transaction escape hatch here
                -- (see comment above EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL
                -- for why none is needed).
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> % -- confirmed is terminal and can never change.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            ELSIF NOT (
                (OLD.parse_status = 'queued' AND
                 NEW.parse_status IN ('parsed', 'needs_review', 'failed'))
                OR (OLD.parse_status = 'needs_review' AND
                    NEW.parse_status = 'parsed')
                OR (OLD.parse_status = 'parsed' AND
                    NEW.parse_status = 'confirmed')
                OR (OLD.parse_status = 'failed' AND
                    NEW.parse_status = 'queued')
            ) THEN
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> %. Legal transitions: queued->parsed, '
                    'queued->needs_review, queued->failed, '
                    'needs_review->parsed, parsed->confirmed, '
                    'failed->queued.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            END IF;
        END IF;

        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id       OR
                NEW.discount_type            IS DISTINCT FROM OLD.discount_type OR
                NEW.discount_value_minor     IS DISTINCT FROM OLD.discount_value_minor OR
                NEW.discount_percent         IS DISTINCT FROM OLD.discount_percent OR
                NEW.discount_threshold_minor IS DISTINCT FROM OLD.discount_threshold_minor OR
                NEW.discount_source          IS DISTINCT FROM OLD.discount_source OR
                NEW.discount_rule_id         IS DISTINCT FROM OLD.discount_rule_id) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id, discount_type, discount_value_minor, '
                    'discount_percent, discount_threshold_minor, '
                    'discount_source, discount_rule_id are immutable once '
                    'confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

# ---------------------------------------------------------------------------
# M6 item 4: GST structured data
# ---------------------------------------------------------------------------
#
# 1. expense_tax_components is a brand-new child table with a DIRECT
#    expense_id FK -- exactly the case reject_mutation_if_expense_confirmed()
#    (migration 0006 / M6 item 1) was already built to support, and exactly
#    the "planned" use case its own docstring called out ahead of time. No
#    escape hatch beyond the existing same-transaction xmin check is needed:
#    unlike item_assignments, nothing appends new expense_tax_components
#    rows to an already-confirmed expense in a later, separate transaction
#    (there is no "tax component correction" flow analogous to refunds), so
#    the refund-style INSERT escape hatch is correctly absent here.
TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL = """
CREATE TRIGGER trg_tax_component_confirm_guard
BEFORE INSERT OR UPDATE OR DELETE ON expense_tax_components
FOR EACH ROW EXECUTE FUNCTION reject_mutation_if_expense_confirmed(
    'expense_id', 'direct'
);
"""

# ---------------------------------------------------------------------------
# M6 item 5: expense_member_allocations -- another brand-new child table
# with a DIRECT expense_id FK, exactly the same shape as
# expense_tax_components above. Reuses reject_mutation_if_expense_confirmed()
# unmodified (no new guard function/version) -- rows are written ONLY inside
# the confirm transaction, AFTER post_expense_to_ledger() has already
# flipped parse_status to 'confirmed' within that same transaction, so the
# existing same-transaction xmin escape hatch covers this insert exactly the
# way it already covers item_assignments' share-freezing insert in the same
# function. Nothing appends new expense_member_allocations rows to an
# already-confirmed expense in a LATER, separate transaction (there is no
# "reallocate after confirm" flow analogous to refunds), so -- like
# expense_tax_components -- no refund-style INSERT escape hatch is needed.
MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_DDL = """
CREATE TRIGGER trg_member_allocation_confirm_guard
BEFORE INSERT OR UPDATE OR DELETE ON expense_member_allocations
FOR EACH ROW EXECUTE FUNCTION reject_mutation_if_expense_confirmed(
    'expense_id', 'direct'
);
"""

# 2. expenses.gst_mode lives directly ON the expenses row (exactly like
#    discount_type/discount_source in migration 0009) -- there is no child
#    table and no FK-join for reject_mutation_if_expense_confirmed() to
#    resolve; OLD/NEW already ARE the expense row. Extending
#    guard_expense_financial_immutability() (CREATE OR REPLACE, same
#    trigger object trg_expense_immutability from migration 0002) is the
#    same-shaped fit used for the discount_* columns, not a new function or
#    new trigger. V4 supersedes V3 exactly as V3 superseded V2.
#
#    expense_line_items.gst_rate / gst_amount_minor are deliberately NOT
#    given a DB-level immutability trigger here (see migration 0010's
#    docstring and app/domain/models.py:ExpenseLineItem's comment on these
#    two columns): expense_line_items as a whole has never had a
#    confirm-guard trigger in this codebase, because
#    POST /expenses/{id}/refunds legitimately INSERTs brand-new
#    expense_line_items rows on an already-confirmed expense (a kind=
#    'refund' line) in a transaction separate from the original confirm.
#    reject_mutation_if_expense_confirmed('expense_id', 'direct') has no
#    refund-shaped escape hatch for that pattern (its only INSERT escape
#    hatch is join_mode='via_line_item' + kind='refund' on
#    item_assignments); naively attaching it to expense_line_items would
#    reject every legitimate refund-line INSERT the moment the parent
#    expense is confirmed, breaking existing, tested behaviour. These two
#    new columns are therefore protected purely at the application layer --
#    they are only ever written inside the same original_status-confirmed
#    guard in app/extraction/tasks.py that already protects every other
#    line-item write, and neither the refund flow nor any other code path
#    touches them.
EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V4 = """
CREATE OR REPLACE FUNCTION guard_expense_financial_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.parse_status = 'confirmed' THEN
            RAISE EXCEPTION
                'Cannot DELETE a confirmed expense (id=%). '
                'Void it by setting status=''voided'' instead.',
                OLD.id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.parse_status IS DISTINCT FROM OLD.parse_status THEN
            IF OLD.parse_status = 'confirmed' THEN
                -- Terminal state: no same-transaction escape hatch here
                -- (see comment above EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL
                -- for why none is needed).
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> % -- confirmed is terminal and can never change.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            ELSIF NOT (
                (OLD.parse_status = 'queued' AND
                 NEW.parse_status IN ('parsed', 'needs_review', 'failed'))
                OR (OLD.parse_status = 'needs_review' AND
                    NEW.parse_status = 'parsed')
                OR (OLD.parse_status = 'parsed' AND
                    NEW.parse_status = 'confirmed')
                OR (OLD.parse_status = 'failed' AND
                    NEW.parse_status = 'queued')
            ) THEN
                RAISE EXCEPTION
                    'Illegal parse_status transition for expense (id=%): '
                    '% -> %. Legal transitions: queued->parsed, '
                    'queued->needs_review, queued->failed, '
                    'needs_review->parsed, parsed->confirmed, '
                    'failed->queued.',
                    OLD.id, OLD.parse_status, NEW.parse_status;
            END IF;
        END IF;

        IF OLD.parse_status = 'confirmed' THEN
            IF (NEW.total_minor    IS DISTINCT FROM OLD.total_minor    OR
                NEW.subtotal_minor IS DISTINCT FROM OLD.subtotal_minor OR
                NEW.paid_by        IS DISTINCT FROM OLD.paid_by        OR
                NEW.currency       IS DISTINCT FROM OLD.currency       OR
                NEW.group_id       IS DISTINCT FROM OLD.group_id       OR
                NEW.discount_type            IS DISTINCT FROM OLD.discount_type OR
                NEW.discount_value_minor     IS DISTINCT FROM OLD.discount_value_minor OR
                NEW.discount_percent         IS DISTINCT FROM OLD.discount_percent OR
                NEW.discount_threshold_minor IS DISTINCT FROM OLD.discount_threshold_minor OR
                NEW.discount_source          IS DISTINCT FROM OLD.discount_source OR
                NEW.discount_rule_id         IS DISTINCT FROM OLD.discount_rule_id OR
                NEW.gst_mode                 IS DISTINCT FROM OLD.gst_mode) THEN
                RAISE EXCEPTION
                    'Cannot mutate financial columns of confirmed expense (id=%). '
                    'Affected columns: total_minor, subtotal_minor, paid_by, '
                    'currency, group_id, discount_type, discount_value_minor, '
                    'discount_percent, discount_threshold_minor, '
                    'discount_source, discount_rule_id, gst_mode are immutable '
                    'once confirmed.',
                    OLD.id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$;
"""

# NOTE: this is the ORIGINAL (M1 hardening / migration 0002) list. Migration
# 0002's upgrade() loops over this exact list — it must stay pinned to these
# four DDL strings forever, regardless of what gets added below, or a
# future `alembic upgrade head` run from scratch would try to (re-)create
# migration 0006+'s trigger objects a second time inside migration 0002 and
# fail with "already exists" (discovered the hard way: see git history of
# this file / migration 0006).
LEDGER_AND_EXPENSE_TRIGGER_DDL: list[str] = [
    LEDGER_TRIGGER_FUNCTION_DDL,
    LEDGER_TRIGGER_DDL,
    EXPENSE_TRIGGER_FUNCTION_DDL,
    EXPENSE_TRIGGER_DDL,
]

# Full list of every Postgres guard DDL statement across all migrations —
# used by tests/conftest.py to install the complete, current set of guards
# for each test run. Individual migrations must import their OWN specific
# DDL constants (as migration 0002 does via LEDGER_AND_EXPENSE_TRIGGER_DDL
# and migration 0006 does via GENERIC_CONFIRM_GUARD_FUNCTION_DDL /
# ITEM_ASSIGNMENT_CONFIRM_GUARD_TRIGGER_DDL) rather than looping over this
# combined list, to avoid re-applying DDL that a later migration already
# owns.
ALL_TRIGGER_DDL: list[str] = [
    *LEDGER_AND_EXPENSE_TRIGGER_DDL,
    GENERIC_CONFIRM_GUARD_FUNCTION_DDL,
    ITEM_ASSIGNMENT_CONFIRM_GUARD_TRIGGER_DDL,
    # M6 item 4: expense_tax_components reuses the same generic function
    # above, unmodified (direct-FK join mode) -- see that DDL's comment.
    TAX_COMPONENT_CONFIRM_GUARD_TRIGGER_DDL,
    # M6 item 5: expense_member_allocations, same generic function, same
    # direct-FK join mode -- see that DDL's comment.
    MEMBER_ALLOCATION_CONFIRM_GUARD_TRIGGER_DDL,
    # Must come AFTER EXPENSE_TRIGGER_FUNCTION_DDL above: each is a CREATE OR
    # REPLACE that upgrades guard_expense_financial_immutability() in place.
    # trg_expense_immutability (created as part of
    # LEDGER_AND_EXPENSE_TRIGGER_DDL) is left untouched throughout -- it
    # already points at this function by name and picks up each new body
    # automatically. Only the LATEST version needs to be applied here for
    # tests (each one fully replaces the function; V4 supersedes V3's body,
    # which supersedes V2's, which supersedes V1's; earlier versions are
    # intentionally NOT also applied first).
    EXPENSE_STATE_MACHINE_GUARD_FUNCTION_DDL_V4,
]
