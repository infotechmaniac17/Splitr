# Deferred Items

Tracked deferrals with risk statements. Deferred items live here in the repo,
not in agent memory. Remove an entry in the same PR that resolves it.

| # | Deferral | Risk | Resolution path |
|---|----------|------|-----------------|
| 1 | **Refresh-token revocation** (M6 item 6, not yet started) | A leaked refresh token remains valid until natural expiry; no server-side kill switch. | Item 6: hashed refresh tokens in DB with revocation on logout/rotation. |
| 2 | **Refund `needs_review` staleness post-confirm** | A refund recorded after confirmation never re-evaluates the expense's GST/discount invariants, so `needs_review` can be stale relative to post-refund arithmetic. | Re-run `check_gst_invariants` after refund posting, or scope the flag as pre-confirm-only in the API contract. |
| 3 | **Allocation-aware refunds** (finance-reviewer finding #3, M6 item 5) | Refund caps/shares use raw item weights and gross line totals; on a discount/GST expense they could move more money than a member actually paid. Currently refunds on such expenses are 409-blocked (`create_refund` guard keyed on nonzero persisted `expense_member_allocations.discount_minor/gst_minor`), so the risk is blocked functionality, not wrong money. | Design per-line net-paid reconstruction from `expense_member_allocations`, then remove the guard and `test_refund_rejected_on_discount_gst_confirmed_expense`. |
| 4 | **item_level discount base is gross (tax-inclusive)** (finance-reviewer finding #4, M6 item 5) | If real vendors compute discount thresholds/percentages on the pre-tax net for item-level-GST invoices, our applied discount and threshold decisions will diverge from the printed invoice by the embedded GST margin. | Verify against real Swiggy/Zomato/Amazon item-level PDFs; see the `DISCOUNT_BEFORE_GST` docstring in `backend/app/domain/splitting.py`. |
