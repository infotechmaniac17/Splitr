# Splitr — Corrected M6/M7 Plan (post-audit)

**Basis:** Codebase audit confirmed the following are DONE and must NOT be rebuilt:
- Alembic: single clean head (0001→0005), no drift
- Debt simplification: `settlement_simplification.py`, real min-cash-flow, tested
- Payments/M8: `Settlement` model, `POST /settlements`, ledger posting — end-to-end
- Auth core: JWT, argon2, roles
- `ItemAssignment` (weight + share_minor), `invoice_date` on `Expense`
- Allocation core: `domain/splitting.py::compute_shares` — pure, largest-remainder, invariant-checked
- API: assignments PATCH, confirm, balances, simplified-debts (existing routes/names)
- Extraction: vendor detection, date parsing, amount-based tax/discount lines
- Frontend: balances page with simplify toggle

**Prime directives for every prompt below:**
1. **EXTEND, don't rebuild.** Before writing code, read the existing module and extend it. Creating a parallel implementation of anything listed above is a defect.
2. **Everything persists in Postgres.** No config in code constants, no in-memory rule tables, no client-computed money. Vendor rules, discount metadata, GST components, assignments, allocation results — all live in the DB via Alembic migrations, with constraints enforced at the DB level (CHECK + triggers), matching the M1–M5 philosophy.
3. `compute_shares` remains the **single** money function. Its existing tests must pass unchanged for the no-discount/no-GST path (regression proof that old confirmed expenses allocate identically).

**Remaining sequence (revised 2026-07-11, after item 5 merged): 5 → 7+8 → 6 → 9.**
Items 7+8 (frontend assignment UI + supporting endpoints) come BEFORE item 6
(refresh-token revocation + ghost implementation). Rationale for the record:
item 6 has no UI dependency and blocks nothing; items 7+8 need only item 5's
allocation-preview plus items 3/4's data, all now live. Visible payoff sooner,
and 7+8's e2e testing exercises item 5's endpoint surface while it's fresh.

---

## Item 1 — DB trigger guard on `item_assignments` (do FIRST)

**Why first:** currently API-layer only — the exact ORM-only-guard bug class the finance-reviewer caught in M1. Items 3–5 add new mutation paths; this closes the gap before they exist. ~Half day.

**Prompt:**
```
Read the existing append-only ledger trigger implementation in our migrations and the
ItemAssignment model before writing anything — mirror that established pattern, do not
invent a new one.

Task: add a DB-level guard so item_assignments rows cannot be INSERTed, UPDATEd, or
DELETEd once their parent expense/invoice is confirmed. Currently this is enforced only
at the API layer, which we know from M1 is insufficient.

Requirements:
1. One new Alembic migration (0006) creating a trigger function + trigger on
   item_assignments (BEFORE INSERT OR UPDATE OR DELETE) that looks up the parent
   expense status and raises an exception if status = 'confirmed'. Follow the naming
   and RAISE-message conventions of our existing append-only triggers.
2. Also guard the reverse path: a trigger or CHECK-equivalent ensuring an expense
   cannot be flipped back from confirmed to draft (verify whether our existing status
   trigger already covers this; if yes, add a test proving it, don't duplicate).
3. Tests against real Postgres via raw psql (not just ORM), matching M1–M5 style:
   - INSERT/UPDATE/DELETE on assignments of a confirmed expense -> rejected at DB level
   - Same operations via SQLAlchemy ORM -> rejected (proves trigger fires regardless
     of access path)
   - Draft expense assignments remain fully mutable
   - Confirm flow itself still works (trigger must not block the confirm transaction's
     own reads)
4. Full existing suite must stay green. Do not modify compute_shares or any API code.
```

---

## Item 2 — Ghost-member DESIGN DECISION (opusplan, design doc only)

**Why now:** `item_assignments` currently hard-FKs to users. If the identity model changes later, item 5's compute_shares extension gets churned. Decide the target schema now; implement in item 6.

**Prompt (opusplan mode):**
```
Design decision only — produce docs/design/ghost-members.md, no implementation.

Problem: item_assignments and group membership currently hard-FK to users, so every
assignee must have an account. We want "ghost members": people in a group (e.g., a
flatmate) who can be assigned items and accrue balances before they ever sign up, and
who can later claim their identity on signup.

Evaluate at least these options against our existing schema (read the actual models
first):
  A. group_members becomes the identity anchor: group_members(id, group_id,
     user_id NULLABLE, display_name, claimed_at NULLABLE); item_assignments,
     ledger entries, and settlements FK to group_members.id instead of users.id.
  B. Placeholder user rows (users with is_ghost flag), FKs unchanged.
  C. Polymorphic assignee (member_type + member_id) — likely reject, justify why.

For the recommended option, specify:
- Exact target schema + migration strategy from current state (data migration for
  existing assignments/ledger rows — everything must remain queryable and balances
  must be provably identical before/after migration)
- Claim flow: ghost signs up -> link user_id, preserve all historical assignments,
  ledger entries, and settlements (append-only ledger must NOT be rewritten; define
  how identity linkage works without mutating ledger rows)
- Impact on compute_shares inputs (member identity type it receives)
- Impact on auth dependencies (require_group_member)
- Uniqueness/merge edge cases: two ghosts claimed by one user, ghost with same email
  as existing user

Deliverable: the doc plus a one-paragraph recommendation. STOP after the doc — I will
review before any code is written.
```

---

## Item 3 — Vendor discount rules (schema + rate/threshold model + matching)

Only genuinely new domain concept. Rules are **data in Postgres**, never code.

**Prompt:**
```
Read the existing extraction vendor-detection code and the Expense/discount handling
first. Then implement vendor discount rules as persistent, DB-stored configuration.

1. Migration 0007: vendor_discount_rules table:
   - id PK, group_id FK NULL (NULL = user-global rule), created_by FK users,
     vendor_pattern TEXT NOT NULL (normalized, matched against our existing
     extraction vendor field — reuse the same normalization function),
     min_order_total_minor BIGINT NOT NULL DEFAULT 0 CHECK (>= 0),
     discount_type TEXT NOT NULL CHECK (discount_type IN ('flat','percent')),
     discount_value_minor BIGINT NULL,        -- for flat, in paise
     discount_percent NUMERIC(5,2) NULL,      -- for percent
     CHECK ((discount_type='flat' AND discount_value_minor > 0 AND discount_percent IS NULL)
         OR (discount_type='percent' AND discount_percent > 0 AND discount_percent <= 100
             AND discount_value_minor IS NULL)),
     active BOOLEAN NOT NULL DEFAULT true, created_at, updated_at
   - Index on (group_id, active, vendor_pattern).
   All money in minor units (paise), consistent with share_minor convention.

2. Migration 0008: discount metadata on Expense (extend, don't replace the existing
   amount-based discount line the extractor already captures):
   - discount_type TEXT NULL CHECK (IN ('flat','percent')),
     discount_value_minor BIGINT NULL, discount_percent NUMERIC(5,2) NULL,
     discount_threshold_minor BIGINT NULL,
     discount_source TEXT NULL CHECK (IN ('manual','vendor_rule','extracted')),
     discount_rule_id FK vendor_discount_rules NULL (ON DELETE SET NULL —
     historical expenses keep their applied discount values even if the rule is
     deleted; the applied amounts are snapshotted on the expense row, never
     re-derived from the rule)
   - Backfill: existing expenses with an extracted discount line get
     discount_source='extracted' with the flat amount.

3. Matching service (pure function + thin DB query layer):
   match_rule(vendor_normalized, subtotal_minor, group_id) -> best active rule or None.
   Precedence: group-scoped rule over global; if multiple, largest applicable
   discount wins; deterministic tie-break by rule id. Threshold check:
   subtotal_minor >= min_order_total_minor (inclusive — 350.00 exactly qualifies
   for the Amazon ₹350/₹50 example).

4. Auto-application hook: when an expense is created/extracted as draft, run
   match_rule and persist the snapshot onto the expense (source='vendor_rule',
   rule_id set). User can override or clear later (item 8 endpoint); auto-apply
   must never overwrite a manual discount already on the draft, and must never
   touch confirmed expenses (the item-1 style guard applies — add a trigger
   preventing discount column mutation on confirmed expenses in this migration).

5. CRUD endpoints: /groups/{group_id}/vendor-discount-rules (list/create/update/
   deactivate — soft delete via active=false, since expenses reference rules).
   Gated by group membership; create/update require owner role.

6. Tests (real Postgres): CHECK constraints via raw psql (flat with percent set ->
   rejected, etc.), threshold boundary at exactly 350.00 and 349.99, group vs
   global precedence, rule deletion leaves historical expense snapshot intact,
   auto-apply skips manual-discount drafts, confirmed-expense discount mutation
   rejected at DB level, seed rule: pattern 'amazon', threshold 35000, flat 5000.

Do NOT touch compute_shares in this item — the discount is persisted on the expense
here; allocation math changes come in item 5.
```

---

## Item 4 — GST as structured data (schema + extraction)

**Prompt:**
```
Read the existing extraction pipeline's tax/discount line handling and the
arithmetic validator before changing anything. We currently capture tax as amount-
based lines; this item upgrades GST to structured, persisted data.

1. Migration 0009:
   - On Expense: gst_mode TEXT NOT NULL DEFAULT 'none'
     CHECK (gst_mode IN ('none','invoice_exclusive','invoice_inclusive','item_level'))
   - New table expense_tax_components:
     id PK, expense_id FK, name TEXT CHECK (name IN ('CGST','SGST','IGST','GST','CESS')),
     rate NUMERIC(5,2) NULL, amount_minor BIGINT NOT NULL CHECK (amount_minor >= 0),
     UNIQUE (expense_id, name)
   - On the invoice item model: gst_rate NUMERIC(5,2) NULL,
     gst_amount_minor BIGINT NULL CHECK (gst_amount_minor >= 0)
   - Trigger: tax components and item GST columns immutable once expense is
     confirmed. Before writing a new trigger function, check whether item 1's
     (migration 0006) guard function can be generalized/reused — a single
     parameterized "reject mutation when parent expense confirmed" function
     shared across item_assignments, expense_tax_components, and item GST
     columns is preferable to three copies. Only write a new one if reuse is
     genuinely awkward.
   - Backfill: existing amount-based tax lines -> one 'GST' component with
     rate NULL, mode 'invoice_exclusive'.

2. Extraction schema (Pydantic structured output) extension:
   - Detect CGST/SGST/IGST/GST/CESS lines with rates when printed; emit components.
   - Detect inclusive vs exclusive ("inclusive of GST", "GST included") -> gst_mode.
   - If per-item GST rates are printed (restaurant 5% food / 18% other), emit
     item-level gst_rate + gst_amount_minor and set mode 'item_level'.
   - Structured discount block for printed coupon/promo lines (feeds the
     discount_source='extracted' columns from item 3).
   All extracted values persist to the new columns/tables on draft creation —
   extraction output is never the source of truth after persistence; the DB row is.

3. Deterministic arithmetic validator — new invariants (exact to the paisa):
   - exclusive: sum(items) - discount + sum(components) == grand_total
   - inclusive: sum(items) - discount == grand_total (components informational,
     must still individually be <= grand_total)
   - item_level: sum(item gst_amount_minor) == sum(components) if both present
   On violation: flag expense for manual review (persisted flag column
   needs_review BOOLEAN on Expense — add in migration 0009); NEVER silently adjust
   numbers.

4. Tests: synthetic fixtures — exclusive single GST, CGST+SGST pair, inclusive,
   item-level mixed 5%/18%, printed discount + GST together, CESS line, and one
   deliberately inconsistent invoice that must set needs_review. Raw psql checks
   on new CHECKs and immutability trigger. Backfill test: pre-existing expense
   with amount-based tax line migrates to a component and validates.

Do NOT touch compute_shares yet.
```

---

## Item 5 — Extend `compute_shares` (discount + GST inputs) — finance-reviewer gate

**Prompt:**
```
This is the money-math change. Read domain/splitting.py::compute_shares and its full
test suite first. EXTEND this function (or wrap it in a thin composing function in the
same module) — do NOT create a parallel allocate_invoice. Two allocation functions is
how penny-mismatch bugs are born.

Extended signature (keep pure — no DB access; callers load rows and pass plain data):
  compute_shares(items, assignments, weights, discount_spec=None, gst_spec=None)
    -> SharesResult (per-member: base_minor, discount_minor, gst_minor, total_minor)

Order of operations (encode as a named strategy constant DISCOUNT_BEFORE_GST=True with
a docstring noting it follows Indian delivery-app convention and is to be re-verified
against real Swiggy/Zomato/Amazon PDFs):
1. Base shares: existing behavior, unchanged — largest-remainder over assigned
   members x weights per item.
2. Discount (flat or percent, threshold-aware): applies only if
   subtotal_minor >= threshold_minor. Distribute across members proportionally to
   pre-discount shares, largest-remainder, stable tie-break by member id.
   INVARIANTS: allocated discount sums exactly to discount amount; no member total
   goes negative (assert — regression guard for the M1 negative-share bypass);
   discount capped at subtotal (a rule can never make grand total negative).
3. GST by mode:
   - invoice_exclusive: total component amount distributed proportionally to
     POST-discount shares, largest-remainder.
   - item_level: each item's gst_amount_minor follows that item's assignment split.
   - invoice_inclusive: no additional allocation (already inside item amounts).
4. Final invariant: sum(member totals) == subtotal - discount + exclusive_gst,
   exact to the paisa. Raise on mismatch, never adjust.

Persistence (everything saved in DB):
- Confirm flow: persist per-member breakdown (base/discount/gst/total minor) — extend
  the existing share_minor storage on assignments/ledger rather than adding a parallel
  store; read the confirm transaction code first and keep it inside the existing
  double-confirm-safe transaction. Confirmed expenses must be fully reconstructable
  from DB rows alone, without re-running allocation.
- New read endpoint GET .../allocation-preview for drafts: loads rows, runs the pure
  function, returns breakdown + validation problems (unassigned items, needs_review).
  Never writes.

Tests:
- ALL existing compute_shares tests pass UNCHANGED (no-discount/no-GST path is
  byte-identical — this is the regression proof for historical expenses).
- Property-based (Hypothesis): random items/weights/discounts/GST modes -> totals
  always reconcile exactly; no negatives; deterministic (same input -> same output).
- Explicit: ₹100/3 with discount, threshold at exactly 350.00 vs 349.99, percent
  discount rounding, mixed 5%/18% item-level GST, exclusive GST after flat discount,
  discount == subtotal (all shares zero, GST on zero), weighted 2:1:1 with discount,
  unassigned item blocks confirm, concurrent double-confirm writes ledger exactly once.

MANDATORY: after implementation, run the finance-logic-reviewer subagent (read-only)
on the full diff. Resolve every finding before merge. Paste its findings and
resolutions into the PR description.
```

---

## Item 6 — Refresh-token revocation + ghost-member implementation

**Prompt:**
```
Two auth extensions to the existing JWT/argon2 implementation — read auth module first.

A. Refresh-token revocation (persisted in DB):
   - Migration: refresh_tokens table (id, user_id FK, token_hash TEXT UNIQUE,
     issued_at, expires_at, revoked_at NULL, replaced_by FK NULL, user_agent/ip
     metadata optional). Tokens stored HASHED only — never plaintext.
   - Rotation: each refresh issues a new token and revokes the old (replaced_by
     chain). Reuse of a revoked token revokes the whole chain (theft detection)
     and is logged.
   - Logout endpoint revokes current chain; logout-all revokes all user tokens.
   - Tests: rotation, revoked-reuse rejection + chain kill, expiry, logout-all,
     access token unaffected by refresh revocation until expiry.

B. Ghost members: implement exactly per the approved docs/design/ghost-members.md
   decision (do not re-litigate the design). Include the data migration with a
   before/after balance-equivalence test: compute all group balances before
   migration, run migration, recompute — must be identical to the paisa. Ledger
   rows must not be mutated. Claim-on-signup flow with tests for the merge edge
   cases listed in the design doc.
```

---

## Item 7+8 — Frontend assignment UI + supporting endpoints (one milestone)

Backend endpoints first (7a), then web UI (7b) — interleave as needed.

**Prompt 7a (backend endpoints):**
```
Read the existing assignments PATCH, confirm, and balances routes first — extend the
existing routers, match existing naming/response conventions.

1. POST .../assignments/bulk: {item_ids, member_ids} — replace-set per item, same
   semantics as the existing single-item PATCH (idempotent; double-call == single-
   call). 409 on confirmed (DB trigger from item 1 is the backstop; return clean
   409 before hitting it).
2. PATCH .../discount on a draft expense: set manual discount (persists to the item-3
   columns, source='manual'), or clear. Clearing re-runs vendor-rule auto-match and
   persists the result. 409 on confirmed.
3. GET /groups/{id}/expenses?from&to&group_by=date — grouped by invoice_date (NOT
   created_at), per-expense per-member share summaries from persisted share rows,
   date boundaries inclusive. Index check: confirm (group_id, invoice_date) index
   exists; add if missing.
Tests: idempotency, 409s, manual-vs-auto discount precedence, date boundaries.
```

**Prompt 7b (Next.js UI — frontend-engineer subagent):**
```
Read the existing balances page for conventions (data-fetching layer, styling,
component patterns) before building. New screens:

1. Invoice review & assignment screen:
   - Items table mirroring PDF column order: [row checkbox] | item | qty | unit
     price | amount | gst rate (when item_level).
   - Per-row member avatar chips (multi-select, checkbox semantics) -> existing
     single-item PATCH, optimistic update + rollback, debounced.
   - Bulk: row checkboxes + "Assign selected to…" -> bulk endpoint.
   - Header: vendor, editable invoice_date when needs_review, discount block
     (vendor-rule badge e.g. "Amazon: ₹50 off on ₹350+ — applied", override/clear
     -> discount PATCH), GST summary from persisted components.
   - Live split panel fed ONLY by GET allocation-preview — never compute money
     client-side. Splitwise-style per-member totals, expandable base/discount/GST
     breakdown, refetch on every assignment/discount change.
   - Confirm disabled with inline reasons (e.g., "2 items unassigned" -> click
     scrolls to first unassigned row); unassigned rows visually flagged.
2. Vendor discount rules screen: list/create/edit/deactivate (owner-gated), showing
   scope (group vs global) and active state.
3. Expenses list: date-grouped (invoice_date) with per-member summaries; wire the
   existing balances page's date filter to the new endpoint.
```

---

## Item 9 — Expo app (greenfield, LAST — after web stabilizes API contracts)

**Prompt (mobile-engineer subagent):**
```
Greenfield Expo app (none exists). Scope for v1: auth (login/refresh against item-6
flow), group list, expense list (date-grouped), invoice review & assignment (card
list: item info + member chips; long-press bulk-select with floating "Assign to…"),
sticky bottom-sheet live split fed by allocation-preview only, balances tab with
simplify toggle (existing endpoint), confirm flow.

Rules: same API contracts as web — zero client-side money math. Offline: queue
assignment PATCHes and replay on reconnect (replace-set makes replay safe); confirm
and discount changes are online-only. Set up the Expo project structure, env-based
API base URL, and secure token storage (expo-secure-store) first.
```

---

## Sequencing & gates

| # | Item | Gate |
|---|------|------|
| 1 | DB trigger guard on item_assignments | Raw-psql trigger tests green; full suite green |
| 2 | Ghost-member design doc | Your review/approval of the doc |
| 3 | Vendor discount rules | CHECK/trigger psql tests; threshold boundary tests; snapshot-survives-rule-deletion test |
| 4 | GST structured data | Validator invariants green; backfill test; needs_review flow |
| 5 | compute_shares extension | **finance-logic-reviewer sign-off**; existing tests pass UNCHANGED; Hypothesis suite green |
| 6 | Token revocation + ghost impl | Balance-equivalence migration test; chain-revocation tests |
| 7+8 | Endpoints + web UI | E2E: upload -> auto-discount badge -> assign -> preview matches confirm -> balances |
| 9 | Expo app | Web API contracts frozen first |

**Persistence audit checklist (verify at every gate):** vendor rules in `vendor_discount_rules`; applied discount snapshotted on `Expense` (survives rule deletion); GST in `expense_tax_components` + item columns; per-member breakdowns persisted at confirm (expense reconstructable from DB without re-running allocation); refresh tokens hashed in DB; `needs_review` persisted; nothing money-related computed or stored client-side.

**Model usage:** opusplan for items 2 and 5 design; Sonnet default elsewhere; finance-logic-reviewer mandatory on item 5 (and advisable on item 3's matching precedence logic).
