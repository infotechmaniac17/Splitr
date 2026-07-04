# Item-Level Expense Splitting Platform — Architecture & Design Document

**Scope:** System architecture, tech stack, PDF extraction strategy, database schema, and splitting algorithm for a cross-platform (Web + Mobile) expense splitting app with invoice-level line-item precision.

---

## 1. Architecture & Tech Stack

### 1.1 High-Level Architecture

```
┌─────────────┐   ┌──────────────┐
│  Web (React)│   │ Mobile (RN)  │
└──────┬──────┘   └──────┬───────┘
       └───────┬─────────┘
               ▼
        ┌─────────────┐        ┌──────────────────┐
        │  API Gateway │──────▶│  Auth (JWT/OAuth) │
        │  (REST/tRPC) │        └──────────────────┘
        └──────┬──────┘
               ▼
   ┌───────────────────────────┐
   │   Core Backend (FastAPI)  │
   │  - Groups/Expenses/Ledger │
   │  - Settlement Engine      │
   └─────┬───────────┬─────────┘
         │           │ enqueue
         ▼           ▼
  ┌────────────┐  ┌─────────────────────────┐
  │ PostgreSQL │  │ Async Workers (Celery)  │
  │ (+ JSONB)  │  │  PDF Extraction Pipeline│
  └────────────┘  └───────┬─────────────────┘
         ▲                │
         │                ▼
  ┌────────────┐   ┌──────────────┐   ┌─────────────┐
  │   Redis    │   │ S3 / GCS     │   │ LLM / OCR   │
  │ cache+queue│   │ (raw PDFs)   │   │ providers   │
  └────────────┘   └──────────────┘   └─────────────┘
```

### 1.2 Recommended Stack

| Layer | Choice | Rationale |
|---|---|---|
| Web frontend | **React + TypeScript (Next.js)** | SSR for fast loads, huge ecosystem, shares types/logic with mobile |
| Mobile | **React Native (Expo)** | Single team, single language; share validation logic, API client, and the item-assignment UI logic via a shared `packages/core` monorepo package |
| Backend | **Python + FastAPI** | The PDF pipeline (pdfplumber, OCR, LLM orchestration) is Python-native; FastAPI gives async I/O, Pydantic schemas that double as LLM structured-output schemas |
| Async processing | **Celery + Redis** (or ARQ) | PDF extraction takes 2–20s; never block a request. Upload → enqueue → notify via WebSocket/push when parsed |
| Database | **PostgreSQL** (see 1.3) | Financial data demands ACID; JSONB covers the "dynamic" parts |
| File storage | S3 / GCS with lifecycle rules | Keep raw PDFs 90 days for re-parse/audit, then archive |
| Realtime | WebSockets (FastAPI) or Firebase/OneSignal push | "Your invoice is ready to split" notifications |

### 1.3 SQL vs NoSQL — the verdict is SQL

This is a **ledger application**. The core invariant — *every rupee on an invoice is assigned to exactly one or more people, and balances always reconcile* — is exactly what relational constraints, foreign keys, and transactions exist for. NoSQL's flexibility for "dynamic item arrays" is a false economy:

- Line items are not schemaless — they always have name, qty, price. That's a table.
- Multi-row atomic updates (create expense + N line items + M assignments + ledger entries) require transactions.
- Settlement queries ("who owes whom across 4 groups") are relational joins/aggregations.

Where genuine dynamism exists — the raw extractor output, vendor-specific fields, parse metadata — use a **JSONB column** (`raw_extraction`) on the expense row. You get Mongo-style flexibility inside Postgres, with the ledger still fully relational.

**Money is stored as `BIGINT` in minor units (paise/cents). Never FLOAT, never NUMERIC-with-rounding-surprises in app code.**

---

## 2. PDF Extraction Deep-Dive

### 2.1 Comparative Analysis

Assumptions: average invoice = 1–3 pages; ~60% digital-text PDFs (Amazon, Flipkart), ~40% image-based or app-generated receipts (Zomato/Swiggy screenshots, scanned bills). Costs are per **1,000 invoices**, approximate and provider-list-price based.

| Criterion | 1. Layout-Aware Document AI (Textract AnalyzeExpense / Google Doc AI Invoice / Azure Doc Intelligence) | 2. LLM with Structured Outputs (GPT-4o / Gemini 1.5–2.x with JSON schema) | 3. Hybrid (pdfplumber/pypdf text → cheap LLM) |
|---|---|---|---|
| **Accuracy — line items** | High on standard tables (95–99% field-level on invoices they're trained for); struggles with Indian food-delivery layouts, multi-column discounts, "₹" glyph issues, and merged fee rows. Deterministic — never invents numbers. | Very high on semantics (understands "Handling charge", BOGO, strikethrough MRP vs selling price); the risk is **numeric hallucination or dropped rows** on long tables. Mitigated by validation (2.2). Vision-capable models handle image PDFs directly. | Highest on digital PDFs (raw text is exact; LLM only structures it — near-zero transcription error). **Fails completely on scanned/image PDFs** (no text layer). |
| **Latency** | 2–8s per doc (async APIs) | 3–15s (vision) / 2–6s (text-only, small model) | 1–4s (text extract is ms; LLM call dominates) |
| **Cost / 1,000 invoices** | Textract AnalyzeExpense ≈ **$10–25**; Google Doc AI Invoice parser ≈ **$30–65** (per-page pricing) | GPT-4o vision ≈ **$5–15**; **Gemini Flash ≈ $0.50–3** (dramatically cheaper, strong on documents) | Text PDFs: **$0.30–1.50** (pdfplumber is free; small LLM on ~2k tokens) |
| **Adaptability to layout changes** (Swiggy redesigns its invoice) | **Poor–moderate.** Pretrained invoice models degrade on novel layouts; custom-trained models need re-labeling and retraining. | **Excellent.** LLMs parse by meaning, not position. A redesign usually needs zero changes; at worst, one prompt tweak. | **Excellent** for the LLM half; pdfplumber layer is layout-agnostic (it just dumps text/tables). |
| **Ops complexity** | Low (managed API) | Low | Moderate (routing logic, two paths) |

### 2.2 The Recommended Architecture: Tiered Hybrid + Deterministic Validation + Human-in-the-Loop

No extractor on earth delivers 100% unsupervised accuracy. The correct engineering framing is: **cheap, adaptive extraction + machine-checkable arithmetic validation + a fast correction UI**. 100% accuracy is achieved *by the system*, not by the model.

```
Upload PDF
   │
   ▼
[Stage 0] Classify: has selectable text layer? (pdfplumber, <100ms)
   │
   ├─ YES (Amazon, Flipkart, most e-invoices)
   │     pdfplumber → raw text + detected tables
   │     → Gemini Flash / GPT-4o-mini with strict JSON Schema
   │       (structured outputs mode: schema-enforced, temp=0)
   │
   └─ NO (scans, screenshots, image-based receipts)
         Vision LLM (Gemini Flash vision) directly on page images
         [fallback: Textract AnalyzeExpense if vision confidence low]
   │
   ▼
[Stage 1] Deterministic Validation Engine (pure code, no AI):
   • Σ(qty × unit_price) per line == line_total (±1 minor unit)
   • Σ(line_totals) + Σ(fees) + Σ(taxes) − Σ(discounts) == invoice_total
   • date parses, currency consistent, qty > 0, no negative unit prices
     (except explicit discount/refund lines)
   │
   ├─ PASSES → status = "parsed", auto-populate split screen
   │            (validation, not model trust, is the accuracy guarantee)
   │
   └─ FAILS  → one retry with the mismatch injected into the prompt
               ("your line items sum to 842 but total is 857 —
                you likely missed a fee row; re-extract")
               → still failing? status = "needs_review":
                 side-by-side UI (PDF left, editable table right),
                 mismatch highlighted, user fixes in ~15 seconds
```

**Why this wins:**

1. **Cost:** blended ≈ **$1–4 per 1,000 invoices** — 10–30× cheaper than Document AI at scale.
2. **Adaptability:** vendor redesigns are absorbed by the LLM's semantic parsing. No retraining, no template maintenance for 9+ vendors × their layout churn.
3. **Trustworthiness:** the arithmetic invariant of an invoice (items + fees − discounts = total) is a *free, perfect checksum*. A hallucinated or dropped line item almost always breaks the equation and gets caught before a user ever sees wrong numbers.
4. **Store everything:** persist the raw model JSON in `expenses.raw_extraction` (JSONB) plus the PDF in S3, so failed parses can be replayed against improved prompts/models later without asking users to re-upload.

Add a lightweight **vendor hint system**: detect vendor from text ("Swiggy", GSTIN patterns) and inject vendor-specific few-shot examples into the prompt. This pushes per-vendor accuracy to near-perfect without any template lock-in.

---

## 3. Database ER Design (PostgreSQL)

```
users ─────────┬──────────────< group_members >──────────── groups
               │                     │                        │
               │                subgroups ──< subgroup_members│
               │                                              │
               │                 expenses >───────────────────┘
               │                    │  (group_id NULL ⇒ personal expense)
               │                    │
               │              expense_line_items
               │                    │
               └──< item_assignments┘
               │
               ├──< ledger_entries          (immutable, append-only)
               └──< settlements             (recorded payments)
```

```sql
users (
  id UUID PK, name, email UNIQUE, phone, avatar_url,
  default_currency CHAR(3) DEFAULT 'INR', created_at
)

groups (
  id UUID PK, name,                       -- "Rent & Groceries", "Goa Trip"
  created_by FK users, simplify_debts BOOL DEFAULT true, created_at
)

group_members (
  group_id FK, user_id FK, role ENUM('admin','member'),
  joined_at, left_at NULL,
  PK (group_id, user_id)
)

subgroups (                               -- "The Vegetarians", "Room 2"
  id UUID PK, group_id FK, name
)
subgroup_members ( subgroup_id FK, user_id FK, PK(subgroup_id, user_id) )

expenses (
  id UUID PK,
  group_id FK NULL,                       -- NULL = personal mode ✔ dual-mode
  paid_by FK users,
  vendor TEXT,                            -- "Swiggy", "Amazon Fresh"
  invoice_date DATE, invoice_number TEXT,
  currency CHAR(3),
  subtotal_minor BIGINT,                  -- Σ item lines, minor units
  total_minor BIGINT,                     -- final charged amount
  source ENUM('pdf','manual'),
  parse_status ENUM('queued','parsed','needs_review','confirmed','failed'),
  pdf_object_key TEXT NULL,               -- S3 key
  raw_extraction JSONB NULL,              -- full model output, audit/replay
  status ENUM('active','voided'),
  created_at, confirmed_at
)

expense_line_items (
  id UUID PK, expense_id FK,
  line_no INT,
  kind ENUM('item','tax','delivery_fee','platform_fee','packing_fee',
            'tip','discount','refund'),
  description TEXT,
  quantity NUMERIC(10,3) DEFAULT 1,       -- supports 0.5 kg
  unit_price_minor BIGINT,
  total_minor BIGINT,                     -- signed: negative for discount/refund
  bundle_group_id UUID NULL,              -- links BOGO/bundle siblings
  parent_line_id FK NULL,                 -- refund → original item
  discount_scope ENUM('item','cart') NULL,
  allocation ENUM('equal','proportional','manual') NULL
                                          -- how fee/discount rows spread
)

item_assignments (
  id UUID PK, line_item_id FK, user_id FK,
  weight NUMERIC(10,4) DEFAULT 1,         -- unequal shares of one item
  share_minor BIGINT,                     -- frozen computed share (audit)
  UNIQUE (line_item_id, user_id)
)
-- Assigning to a subgroup = UI sugar: expands to one row per member.

ledger_entries (                          -- append-only source of truth
  id UUID PK, group_id FK NULL,
  expense_id FK NULL, settlement_id FK NULL,
  debtor_id FK users, creditor_id FK users,
  amount_minor BIGINT CHECK (amount_minor > 0),
  entry_type ENUM('expense_share','refund_reversal','settlement','adjustment'),
  created_at
)

settlements (
  id UUID PK, group_id FK NULL,
  payer_id FK, payee_id FK, amount_minor BIGINT,
  method ENUM('upi','cash','bank','other'), note, settled_at
)

balances (MATERIALIZED VIEW or cached table)
  -- Σ ledger_entries grouped by (group_id, debtor, creditor)
  -- Netted pairwise; "simplify debts" runs min-cash-flow on this graph.
```

**Design principles baked in:**

- **Immutability for money.** `expenses` and `ledger_entries` are never destructively edited after confirmation. Corrections, refunds, and voids create *new* signed entries. This gives a complete audit trail and makes "the bill was already split, then something changed" a solved problem rather than a data-corruption risk.
- **`share_minor` is frozen at confirmation** so historical balances never shift retroactively if splitting rules change.
- **Settlement engine:** net pairwise balances per group; if `simplify_debts`, run greedy min-cash-flow (repeatedly match largest debtor with largest creditor) — O(n log n), provably ≤ n−1 transactions.

### Edge-Case Handling

| Scenario | Mechanism |
|---|---|
| **Return/refund after split** | New line item `kind='refund'`, `parent_line_id` → original item, negative `total_minor`. Its assignments **copy the original item's assignment ratios**, producing `refund_reversal` ledger entries that flow money back along exactly the path it came. If a payout went to the payer's account, the reversal credits assignees against the payer. Already-settled debts aren't rewritten — the reversal simply shifts the *current* net balance. |
| **BOGO / bundles** | Free unit stored as its own line: qty 1, `unit_price_minor = 0`, sharing a `bundle_group_id` with the paid unit. UI renders the bundle as one card ("Buy 1 Get 1 — Pizza ×2") but lets Item A go to Alice and the free one to Bob. Cost attribution options: free-item-costs-zero (default) or redistribute bundle price across units. Bundled combos ("Meal for 2 @ ₹399") = one line, qty 1, assignable to multiple users. |
| **Digital vs scanned PDFs** | Stage-0 router in the pipeline (§2.2). Both converge on the same JSON schema and validation engine. |
| **Corrupted/unsupported PDF — manual fallback** | `parse_status='failed'` opens the **Quick Manual Entry** flow: (1) total-first ("₹857 at Swiggy") is enough to save and split equally immediately; (2) optional rapid line-item mode — single-row form with autocomplete from the user's item history, `Enter` adds the next row; (3) a running "Unassigned: ₹212" chip shows the gap between entered lines and the total, and one tap dumps the remainder into a "Misc" line. Never block the user on parsing. |

---

## 4. The Splitting Algorithm

### Worked Example
Cart: **User A = ₹20** of items, **User B = ₹40** of items → subtotal **₹60**.
Cart-level discount **−₹10**, delivery fee **+₹3**. Invoice total **₹53**.

**Step 1 — item shares** (from `item_assignments`): A = 2000, B = 4000 (minor units).
**Step 2 — proportions:** A = 2000/6000 = ⅓, B = ⅔.

**Step 3 — cart discount, `allocation='proportional'`:**
- A: −1000 × ⅓ = **−333** → but ⅓ of 1000 isn't clean. Compute exact: A = −333.33, B = −666.67.
- **Largest-remainder rounding:** floor both (−333, −666), 1 paisa unallocated, give it to the largest fractional remainder (B) → A **−333**, B **−667**. Sums to exactly −1000. ✔

**Step 4 — delivery fee ₹3 (300):**
- If `proportional`: A = 300 × ⅓ = **100**, B = **200**.
- If `equal`: A = **150**, B = **150**.

**Step 5 — final (proportional fees):**
- A: 2000 − 333 + 100 = **₹17.67**
- B: 4000 − 667 + 200 = **₹35.33**
- Check: 17.67 + 35.33 = **₹53.00** = invoice total. ✔ (This equality is asserted in code; a failure aborts the transaction.)

### Pseudocode

```python
def compute_shares(expense) -> dict[user_id, int]:   # minor units
    shares = defaultdict(int)

    # 1. Direct item lines (kind == 'item', and item-scoped discounts/refunds)
    item_lines = [l for l in expense.lines if l.kind == 'item'
                  or l.discount_scope == 'item' or l.kind == 'refund']
    for line in item_lines:
        split_among_assignees(line, shares)          # weight-based, largest-remainder

    item_subtotal = sum(shares.values())
    proportions = {u: shares[u] / item_subtotal for u in shares}

    # 2. Cart-level rows: fees, taxes, cart discounts
    for line in expense.lines - item_lines:
        if line.allocation == 'equal':
            targets = {u: 1 / len(shares) for u in shares}
        elif line.allocation == 'manual':
            targets = line.manual_ratios
        else:                                        # proportional (default)
            targets = proportions
        allocate_largest_remainder(line.total_minor, targets, shares)

    assert sum(shares.values()) == expense.total_minor   # hard invariant
    return shares

def allocate_largest_remainder(amount, ratios, shares):
    exact  = {u: amount * r for u, r in ratios.items()}
    floors = {u: trunc_toward_zero(v) for u, v in exact.items()}
    residual = amount - sum(floors.values())          # 0..n-1 minor units
    for u in sorted(ratios, key=lambda u: exact[u] - floors[u], reverse=True):
        if residual == 0: break
        floors[u] += sign(amount); residual -= sign(amount)
    for u, v in floors.items(): shares[u] += v

def post_to_ledger(expense, shares):
    with db.transaction():                            # atomic
        for user, amt in shares.items():
            if user != expense.paid_by and amt != 0:
                insert ledger_entry(debtor=user, creditor=expense.paid_by,
                                    amount=amt, type='expense_share')
        freeze share_minor on item_assignments
        expense.parse_status = 'confirmed'
```

**Key guarantees:** all arithmetic in integer minor units; largest-remainder rounding means allocated shares *always* sum exactly to the source amount; the final assertion mirrors the PDF-validation invariant, so the same checksum protects the money at both ends of the pipeline — extraction and splitting.

---

## 5. Suggested Build Order

1. **Core ledger + manual expenses + settlement engine** (the app is useful with zero AI).
2. **PDF pipeline for digital PDFs** (pdfplumber + Gemini Flash + validation) — covers Amazon/Flipkart fast.
3. **Vision path** for Zomato/Swiggy/quick-commerce receipts.
4. **Review UI + vendor hints** — this is where perceived "100% accuracy" is won.
5. Refunds, BOGO UI, subgroups, debt simplification polish.
