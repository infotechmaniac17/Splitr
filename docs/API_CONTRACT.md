# Splitr API Contract — Expenses / Line Items / Extraction (v1)

**Status:** Frozen for M3. This is what M4 (web) and M5 (mobile) build
against for the upload → parse → correct → split flow. Backward-incompatible
changes to any shape below require bumping to a `v2` contract, not silently
editing this one.

Source of truth for field names/enums: `docs/ARCHITECTURE.md` §2.2 (pipeline)
and §3 (schema). This document is the *API-facing* projection of that schema
— i.e. what clients actually see over HTTP/JSON, not the ORM/DB layer.

---

## 1. `parse_status` — state machine

```
queued ──> parsed ──> confirmed
   │           │
   │           └──> (refunds/edits happen pre-confirm; see M2 endpoints)
   │
   └──> needs_review ──> (user corrects in UI) ──> confirmed
   │
   └──> failed   (corrupted/unsupported PDF — Quick Manual Entry, ARCHITECTURE.md §3)
```

| Value | Meaning | Set by |
|---|---|---|
| `queued` | Expense row created (PDF uploaded), extraction not yet run. | Upload endpoint (M4, out of scope here) |
| `parsed` | Extraction ran and **passed** the deterministic validation engine, OR this is a manually-entered expense. Line items are trustworthy and ready for the assignment screen. | `app.extraction.pipeline.run_extraction_pipeline` (PDF) / expense-create endpoint (manual) — **never** set anywhere else |
| `needs_review` | Extraction ran but failed validation after one retry-with-mismatch, OR no extraction provider was configured/available. Line items may be incomplete or arithmetically inconsistent — the correction UI must be shown before the user can proceed to assignment/confirm. | `app.extraction.pipeline.run_extraction_pipeline` |
| `confirmed` | Shares computed and posted to the ledger. `expenses` and its `line_items`/`assignments` are now append-only in spirit — corrections are new signed entries, not edits (invariant #2). | `POST /expenses/{id}/confirm` |
| `failed` | PDF could not be parsed at all (corrupted/unsupported). Opens Quick Manual Entry (ARCHITECTURE.md §3 edge case table). | Upload/parse pipeline infra failure (not the same as `needs_review`, which means "we got JSON but it didn't reconcile") |

**Invariant enforced by code, not convention:** `parse_status='parsed'` can
only be produced by a path that has run
`app.extraction.validation.validate_extraction()` and gotten `ok=True` (see
`app/extraction/pipeline.py`). No endpoint or task is allowed to set
`parsed` any other way for PDF-sourced expenses.

---

## 2. `GET /expenses/{id}` response shape

```jsonc
{
  "id": "uuid",
  "group_id": "uuid | null",
  "paid_by": "uuid",
  "vendor": "string | null",          // e.g. "Swiggy" — from extraction or manual entry
  "invoice_date": "2026-06-20 | null",
  "invoice_number": "string | null",
  "currency": "INR",
  "subtotal_minor": 38400,             // integer, minor units; null until parsed
  "total_minor": 36000,                // integer, minor units — BIGINT, never float
  "source": "pdf | manual",
  "parse_status": "queued | parsed | needs_review | confirmed | failed",
  "status": "active | voided",
  "created_at": "2026-06-20T10:00:00Z",
  "confirmed_at": "2026-06-20T10:05:00Z | null",
  "line_items": [
    {
      "id": "uuid",
      "expense_id": "uuid",
      "line_no": 1,
      "kind": "item | tax | delivery_fee | platform_fee | packing_fee | tip | discount | refund",
      "description": "string | null",
      "quantity": "3.000",             // string-serialized Decimal (NUMERIC(10,3))
      "unit_price_minor": 4500,        // integer | null
      "total_minor": 13500,            // integer, SIGNED (negative for discount/refund)
      "allocation": "equal | proportional | manual | null",
      "discount_scope": "item | cart | null",
      "parent_line_id": "uuid | null",
      "bundle_group_id": "uuid | null"
    }
    // ...
  ]
}
```

`raw_extraction` (JSONB) is **intentionally NOT included** in the default
`ExpenseResponse` payload — it is large, provider-specific, and not needed
by the assignment screen. It is exposed separately (§4) for the
correction/audit UI.

---

## 3. `raw_extraction` JSONB shape (audit / replay)

Written exactly once per pipeline run by `app.extraction.tasks`. Never
mutated in place afterward — a re-parse fully replaces it (and the
`expense_line_items` rows) rather than patching either. Persisted so failed
parses can be replayed against improved prompts/models later without
re-asking the user to upload (ARCHITECTURE.md §2.2 point 4).

```jsonc
{
  "attempts": [
    {
      "attempt": 1,
      "provider": "gemini | openai | none | scripted-in-tests",
      "route": "text | vision",
      "raw": { /* verbatim provider JSON, matches ExtractedInvoice schema */ },
      "validation": {
        "ok": false,
        "issues": [
          {
            "code": "invoice_total_mismatch",
            "message": "line items sum to 16500 but invoice total is 19000 — you likely missed a fee, tax, or discount row; re-extract.",
            "line_no": null
          }
        ]
      }
      // "error" and no "raw"/"validation" keys instead, if the provider call itself failed:
      // "error": "GEMINI_API_KEY not set; Gemini provider unavailable."
    },
    {
      "attempt": 2,
      "provider": "gemini",
      "route": "text",
      "raw": { /* retried JSON, with the mismatch injected into the prompt */ },
      "validation": { "ok": true, "issues": [] }
    }
  ],
  // only present if the FIRST attempt's provider call itself failed
  // (no retry is attempted against a provider that just told us it can't run):
  "final_error": "GEMINI_API_KEY not set; Gemini provider unavailable."
}
```

Notes for consumers:
- At most 2 attempts (`MAX_ATTEMPTS` in `app/extraction/pipeline.py`) —
  Stage 1's "one retry with the mismatch injected" (ARCHITECTURE.md §2.2).
- `attempts[].validation.issues[].code` is a stable, machine-readable enum
  (see §4) — build UI highlighting off `code`, not off `message` text.
- `raw` under each attempt is the **unvalidated** model output — do not
  trust its arithmetic; only `expense.line_items` (persisted after the
  pipeline's own validation pass) should drive the split UI for `parsed`
  expenses. For `needs_review` expenses, `line_items` reflects the *last*
  attempt's (possibly-wrong) extraction — the correction UI must let the
  user edit every field.

---

## 4. `needs_review` correction-UI payload

M4's side-by-side correction screen (PDF left, editable table right,
mismatch highlighted — ARCHITECTURE.md §2.2) needs three things, all
already available from existing endpoints — no new endpoint required for M3:

1. **PDF reference** — `expense.pdf_object_key` (S3 key; upload/serving is
   out of scope for M3, wired in M4).
2. **Which line/field mismatched** — `expense.raw_extraction.attempts[-1].validation.issues`,
   each with:
   - `code`: stable enum, one of:
     `no_line_items | bad_quantity | negative_unit_price | sign_convention | line_arithmetic | invoice_total_mismatch | currency_unrecognized | bad_date`
   - `message`: human-readable explanation (safe to render directly)
   - `line_no`: `int | null` — `null` means an invoice-level issue (e.g.
     `invoice_total_mismatch`, `currency_unrecognized`), not a specific line.
3. **Editable fields** — `expense.line_items[]` (§2 shape) is the working
   set the user edits. `line_no`, `kind`, `description`, `quantity`,
   `unit_price_minor`, `total_minor` are all directly editable; `kind` must
   stay within the `LineItemKind` enum; sign convention
   (`discount`/`refund` ⇒ `total_minor <= 0`) is re-checked by
   `validate_extraction`-equivalent logic when the user resubmits (M4 should
   call the same deterministic checks client-side for instant feedback, but
   the server-side validation engine remains the sole authority).

**Not yet built (explicitly out of M3 scope):** the endpoint that accepts
the user's corrected line items and transitions `needs_review -> parsed`.
M4 will need something like `PUT /expenses/{id}/line-items` that re-runs
`app.extraction.validation.validate_extraction` server-side against the
user-submitted values before allowing the transition — flagged here so M4
doesn't invent an endpoint that bypasses the validation engine.

---

## 5. Versioning

This is contract **v1**. Additive, backward-compatible changes (new optional
fields, new enum values appended at the end) may be made in place. Any
change that would break an existing client (renaming/removing a field,
changing a type, changing enum semantics) must be introduced as a new
`v2` document and both maintained until clients migrate.
