/**
 * Fetch-based API client for the Splitr backend. Mirrors
 * backend/app/api/router.py's `/api/v1` prefix and each sub-router exactly
 * (docs/API_CONTRACT.md + backend/app/api/*.py). Usable from web (Next.js)
 * and, later, mobile (React Native) — no DOM/Next-specific APIs used here.
 *
 * Auth: the backend now has real auth (backend/app/api/auth.py) --
 * register/login/refresh issue a short-lived JWT access token (15 min) and
 * a longer-lived refresh token (30 days); see schemas.ts's comment above
 * tokenResponseSchema for the exact shapes/lifetimes. Money-mutating
 * endpoints (create/confirm an expense, assignments, refunds, settlements,
 * groups) now require `Authorization: Bearer <access_token>` AND reject
 * requests where a client-submitted actor id (paid_by/created_by/payer_id)
 * doesn't match the authenticated caller (or isn't a group member) — see
 * app/api/deps.py:get_current_user and the _assert_actor_authorized_for_*
 * helpers in app/api/expenses.py / groups.py / settlements.py.
 *
 * This client accepts an optional `getAccessToken` hook (set once via
 * `setAccessTokenProvider`, or per-instance via the constructor) so the
 * caller doesn't have to pass a header on every method call. Replaces the
 * web app's previous client-side-only "who are you" localStorage picker
 * (web/src/lib/current-user.tsx) — that file is being migrated to call
 * register/login/refresh/me below instead.
 */

import {
  type AccessTokenResponse,
  type AllocationPreviewResponse,
  type AssignmentResponse,
  type AssignmentsPut,
  type BulkAssignmentIn,
  type ExpenseCreate,
  type ExpenseDiscountPatch,
  type ExpenseResponse,
  type GroupBalancesResponse,
  type GroupCreate,
  type GroupExpensesGroupedResponse,
  type GroupMemberAdd,
  type GroupMemberResponse,
  type GroupMembersResponse,
  type GroupResponse,
  type LineItemsCorrection,
  type LoginRequest,
  type RawExtraction,
  type RefreshRequest,
  type RefundCreate,
  type RegisterRequest,
  type SettlementCreate,
  type SettlementResponse,
  type SharesResponse,
  type TokenResponse,
  type UserBalanceResponse,
  type UserCreate,
  type UserResponse,
  type VendorDiscountRuleCreate,
  type VendorDiscountRuleResponse,
  type VendorDiscountRulesListResponse,
  type VendorDiscountRuleUpdate,
  accessTokenResponseSchema,
  allocationPreviewResponseSchema,
  assignmentResponseSchema,
  expenseResponseSchema,
  groupBalancesResponseSchema,
  groupExpensesGroupedResponseSchema,
  groupMemberResponseSchema,
  groupMembersResponseSchema,
  groupResponseSchema,
  rawExtractionSchema,
  settlementResponseSchema,
  sharesResponseSchema,
  tokenResponseSchema,
  userBalanceResponseSchema,
  userResponseSchema,
  vendorDiscountRuleResponseSchema,
  vendorDiscountRulesListResponseSchema,
} from "./schemas";
import { z } from "zod";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `API error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  /**
   * Called before every request to obtain the current access token (or
   * `null`/`undefined` if signed out). Typically backed by whatever
   * in-memory/secure-storage session store the app keeps (see the
   * replacement for web/src/lib/current-user.tsx) -- this client never
   * persists tokens itself.
   */
  getAccessToken?: () => string | null | undefined;
}

async function parseJsonOrThrow<T>(
  res: Response,
  schema: z.ZodType<T, z.ZodTypeDef, any>,
): Promise<T> {
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : body;
    throw new ApiError(res.status, detail);
  }
  return schema.parse(body);
}

export class SplitrApiClient {
  private baseUrl: string;
  private fetchImpl: typeof fetch;
  private getAccessToken?: () => string | null | undefined;

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    // Native fetch is spec'd to require its receiver to be a Window/
    // WorkerGlobalScope; storing the bare function and later invoking it as
    // `this.fetchImpl(...)` (a method call off this class instance) throws
    // "Failed to execute 'fetch' on 'Window': Illegal invocation" in every
    // real browser. Bind it to globalThis so it carries its own receiver.
    this.fetchImpl = options.fetchImpl ?? fetch.bind(globalThis);
    this.getAccessToken = options.getAccessToken;
  }

  private url(path: string): string {
    return `${this.baseUrl}/api/v1${path}`;
  }

  private authHeaders(): Record<string, string> {
    const token = this.getAccessToken?.();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  private async request<T>(
    method: string,
    path: string,
    // `Input = any` (rather than the default `Input = T`) is deliberate:
    // some response schemas below preprocess/default individual fields
    // (e.g. schemas.ts's decimalDisplayString, or an array field backed by
    // Field(default_factory=list) on the Pydantic side), which makes a
    // schema's INPUT type differ from its OUTPUT type. Binding Input=T too
    // (the z.ZodType<T> default) makes TypeScript infer T from the
    // (optionally-undefined/looser) input side instead of the actual
    // parsed output shape at every call site below -- decoupling Input
    // here fixes that class of bug across the whole client in one place.
    schema: z.ZodType<T, z.ZodTypeDef, any>,
    body?: unknown,
    init?: RequestInit,
  ): Promise<T> {
    const res = await this.fetchImpl(this.url(path), {
      method,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...this.authHeaders(),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...init,
    });
    return parseJsonOrThrow(res, schema);
  }

  // -- Auth --------------------------------------------------------------
  //
  // register/login return a full access+refresh token pair (see
  // schemas.ts's tokenResponseSchema doc comment for exact lifetimes).
  // Callers are responsible for persisting both tokens (e.g. secure
  // storage on mobile, an httpOnly-cookie-backed session or in-memory
  // store on web) and wiring `getAccessToken` (constructor option above)
  // to read the access token back for subsequent requests.

  register(payload: RegisterRequest): Promise<TokenResponse> {
    return this.request("POST", "/auth/register", tokenResponseSchema, payload);
  }

  login(payload: LoginRequest): Promise<TokenResponse> {
    return this.request("POST", "/auth/login", tokenResponseSchema, payload);
  }

  refresh(payload: RefreshRequest): Promise<AccessTokenResponse> {
    return this.request(
      "POST",
      "/auth/refresh",
      accessTokenResponseSchema,
      payload,
    );
  }

  getCurrentUser(): Promise<UserResponse> {
    return this.request("GET", "/auth/me", userResponseSchema);
  }

  // -- Users -----------------------------------------------------------

  createUser(payload: UserCreate): Promise<UserResponse> {
    return this.request("POST", "/users", userResponseSchema, payload);
  }

  getUser(userId: string): Promise<UserResponse> {
    return this.request("GET", `/users/${userId}`, userResponseSchema);
  }

  getUserBalance(userId: string): Promise<UserBalanceResponse> {
    return this.request(
      "GET",
      `/users/${userId}/balance`,
      userBalanceResponseSchema,
    );
  }

  // -- Groups ------------------------------------------------------------

  createGroup(payload: GroupCreate): Promise<GroupResponse> {
    return this.request("POST", "/groups", groupResponseSchema, payload);
  }

  getGroup(groupId: string): Promise<GroupResponse> {
    return this.request("GET", `/groups/${groupId}`, groupResponseSchema);
  }

  addGroupMember(
    groupId: string,
    payload: GroupMemberAdd,
  ): Promise<GroupMemberResponse> {
    return this.request(
      "POST",
      `/groups/${groupId}/members`,
      groupMemberResponseSchema,
      payload,
    );
  }

  getGroupMembers(groupId: string): Promise<GroupMembersResponse> {
    return this.request(
      "GET",
      `/groups/${groupId}/members`,
      groupMembersResponseSchema,
    );
  }

  getGroupBalances(groupId: string): Promise<GroupBalancesResponse> {
    return this.request(
      "GET",
      `/groups/${groupId}/balances`,
      groupBalancesResponseSchema,
    );
  }

  /**
   * M6-M8 item 7a: expenses grouped by invoice_date. `from`/`to` are
   * inclusive YYYY-MM-DD date-range filters against invoice_date; an
   * expense with no known invoice_date is placed in its own `date: null`
   * bucket and is NEVER excluded by the range filter (see
   * backend/app/api/groups.py:get_group_expenses_grouped). Per-member
   * summaries in the response are read from persisted rows only -- never
   * recompute them client-side.
   */
  getGroupExpenses(
    groupId: string,
    params?: { from?: string; to?: string },
  ): Promise<GroupExpensesGroupedResponse> {
    const query = new URLSearchParams();
    if (params?.from) query.set("from", params.from);
    if (params?.to) query.set("to", params.to);
    query.set("group_by", "date");
    return this.request(
      "GET",
      `/groups/${groupId}/expenses?${query.toString()}`,
      groupExpensesGroupedResponseSchema,
    );
  }

  // -- Vendor discount rules (M6 item 3 / M6-M8 item 7a UI) -----------------
  //
  // Two families: group-scoped (/groups/{id}/vendor-discount-rules[/...])
  // and creator-global (/vendor-discount-rules/global[/...]). See
  // backend/app/api/vendor_discount_rules.py's module docstring for the
  // admin-only-for-group-scoped / creator-only-for-global authorization
  // rules -- this client doesn't enforce those, it just surfaces whatever
  // 403 the backend returns.

  listGroupVendorDiscountRules(
    groupId: string,
  ): Promise<VendorDiscountRulesListResponse> {
    return this.request(
      "GET",
      `/groups/${groupId}/vendor-discount-rules`,
      vendorDiscountRulesListResponseSchema,
    );
  }

  createGroupVendorDiscountRule(
    groupId: string,
    payload: VendorDiscountRuleCreate,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "POST",
      `/groups/${groupId}/vendor-discount-rules`,
      vendorDiscountRuleResponseSchema,
      payload,
    );
  }

  updateGroupVendorDiscountRule(
    groupId: string,
    ruleId: string,
    payload: VendorDiscountRuleUpdate,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "PUT",
      `/groups/${groupId}/vendor-discount-rules/${ruleId}`,
      vendorDiscountRuleResponseSchema,
      payload,
    );
  }

  deactivateGroupVendorDiscountRule(
    groupId: string,
    ruleId: string,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "DELETE",
      `/groups/${groupId}/vendor-discount-rules/${ruleId}`,
      vendorDiscountRuleResponseSchema,
    );
  }

  listGlobalVendorDiscountRules(): Promise<VendorDiscountRulesListResponse> {
    return this.request(
      "GET",
      "/vendor-discount-rules/global",
      vendorDiscountRulesListResponseSchema,
    );
  }

  createGlobalVendorDiscountRule(
    payload: VendorDiscountRuleCreate,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "POST",
      "/vendor-discount-rules/global",
      vendorDiscountRuleResponseSchema,
      payload,
    );
  }

  updateGlobalVendorDiscountRule(
    ruleId: string,
    payload: VendorDiscountRuleUpdate,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "PUT",
      `/vendor-discount-rules/global/${ruleId}`,
      vendorDiscountRuleResponseSchema,
      payload,
    );
  }

  deactivateGlobalVendorDiscountRule(
    ruleId: string,
  ): Promise<VendorDiscountRuleResponse> {
    return this.request(
      "DELETE",
      `/vendor-discount-rules/global/${ruleId}`,
      vendorDiscountRuleResponseSchema,
    );
  }

  // -- Expenses ------------------------------------------------------------

  createExpense(payload: ExpenseCreate): Promise<ExpenseResponse> {
    return this.request("POST", "/expenses", expenseResponseSchema, payload);
  }

  getExpense(expenseId: string): Promise<ExpenseResponse> {
    return this.request("GET", `/expenses/${expenseId}`, expenseResponseSchema);
  }

  confirmExpense(expenseId: string): Promise<ExpenseResponse> {
    return this.request(
      "POST",
      `/expenses/${expenseId}/confirm`,
      expenseResponseSchema,
    );
  }

  putAssignments(
    expenseId: string,
    payload: AssignmentsPut,
  ): Promise<AssignmentResponse[]> {
    return this.request(
      "PUT",
      `/expenses/${expenseId}/assignments`,
      z.array(assignmentResponseSchema),
      payload,
    );
  }

  getShares(expenseId: string): Promise<SharesResponse> {
    return this.request(
      "GET",
      `/expenses/${expenseId}/shares`,
      sharesResponseSchema,
    );
  }

  createRefund(
    expenseId: string,
    payload: RefundCreate,
  ): Promise<ExpenseResponse> {
    return this.request(
      "POST",
      `/expenses/${expenseId}/refunds`,
      expenseResponseSchema,
      payload,
    );
  }

  /**
   * M6-M8 item 7a convenience bulk-assign: replace-set semantics PER ITEM
   * (only the targeted `item_ids`' assignments are replaced; other line
   * items on the expense are left untouched) -- see
   * backend/app/api/expenses.py:bulk_put_assignments. Used both for the
   * assignment screen's multi-select "assign selected to..." action AND
   * (with a single item id) as the per-row avatar-toggle mutation, since
   * there is no dedicated single-line PUT route.
   */
  bulkPutAssignments(
    expenseId: string,
    payload: BulkAssignmentIn,
  ): Promise<AssignmentResponse[]> {
    return this.request(
      "POST",
      `/expenses/${expenseId}/assignments/bulk`,
      z.array(assignmentResponseSchema),
      payload,
    );
  }

  /**
   * Set/clear an expense's discount snapshot (draft expenses only). See
   * backend/app/api/expenses.py:patch_expense_discount for precedence rules
   * (manual always wins; clearing re-runs vendor-rule auto-matching) and the
   * 422 for expenses whose shares were frozen at create time.
   */
  patchExpenseDiscount(
    expenseId: string,
    payload: ExpenseDiscountPatch,
  ): Promise<ExpenseResponse> {
    return this.request(
      "PATCH",
      `/expenses/${expenseId}/discount`,
      expenseResponseSchema,
      payload,
    );
  }

  /**
   * M6 item 5: live (draft) or persisted (confirmed) per-member discount +
   * GST breakdown. NEVER computed client-side -- see
   * backend/app/api/expenses.py:get_allocation_preview.
   */
  getAllocationPreview(expenseId: string): Promise<AllocationPreviewResponse> {
    return this.request(
      "GET",
      `/expenses/${expenseId}/allocation-preview`,
      allocationPreviewResponseSchema,
    );
  }

  /**
   * M6-M8 total-reconciliation ruling, item 3: recompute the reconciled
   * total (base item totals minus the effective discount, plus tax
   * components for gst_mode='invoice_exclusive') from the expense's
   * CURRENTLY PERSISTED line items / discount snapshot / tax components,
   * and overwrite total_minor with it -- the ONLY sanctioned way
   * total_minor changes on a draft expense. Draft expenses only (409
   * confirmed/voided, 422 frozen shares) -- see
   * backend/app/api/expenses.py:accept_computed_total.
   */
  acceptComputedTotal(expenseId: string): Promise<ExpenseResponse> {
    return this.request(
      "POST",
      `/expenses/${expenseId}/accept-computed-total`,
      expenseResponseSchema,
    );
  }

  // -- Settlements ---------------------------------------------------------

  createSettlement(payload: SettlementCreate): Promise<SettlementResponse> {
    return this.request(
      "POST",
      "/settlements",
      settlementResponseSchema,
      payload,
    );
  }

  // -- PDF upload / needs-review correction ---------------------------------
  //
  // M4 ASSUMPTION (see schemas.ts comment above expenseUploadResponseSchema):
  // these two routes are not yet implemented on the backend. Contract
  // chosen to be consistent with API_CONTRACT.md's own description of what
  // M4 will need:
  //   - "Upload endpoint (M4, out of scope here)" -> POST /expenses/upload
  //   - "M4 will need something like PUT /expenses/{id}/line-items" (§4)
  // Both calls will 404 against the current backend until it lands; the web
  // app treats a 404 here as a clear "not implemented yet" state rather than
  // masking it.

  uploadExpensePdf(params: {
    file: Blob;
    filename: string;
    paidBy: string;
    groupId?: string | null;
    vendorHint?: string | null;
  }): Promise<ExpenseResponse> {
    const form = new FormData();
    form.append("file", params.file, params.filename);
    form.append("paid_by", params.paidBy);
    if (params.groupId) form.append("group_id", params.groupId);
    if (params.vendorHint) form.append("vendor_hint", params.vendorHint);

    // Do NOT set Content-Type manually — the browser/runtime sets the
    // multipart boundary for FormData bodies. `paid_by` must match the
    // authenticated caller (see class doc comment above).
    return this.fetchImpl(this.url("/expenses/upload"), {
      method: "POST",
      headers: this.authHeaders(),
      body: form,
    }).then((res) => parseJsonOrThrow(res, expenseResponseSchema));
  }

  submitLineItemCorrection(
    expenseId: string,
    payload: LineItemsCorrection,
  ): Promise<ExpenseResponse> {
    return this.request(
      "PUT",
      `/expenses/${expenseId}/line-items`,
      expenseResponseSchema,
      payload,
    );
  }

  /**
   * M4 ASSUMPTION: API_CONTRACT.md §3 documents the `raw_extraction` JSONB
   * shape and says it's "exposed separately ... for the correction/audit
   * UI" but never names the route. Assumed path below, consistent with the
   * REST style of every other per-expense sub-resource in this client.
   */
  getRawExtraction(expenseId: string): Promise<RawExtraction> {
    return this.request(
      "GET",
      `/expenses/${expenseId}/raw-extraction`,
      rawExtractionSchema,
    );
  }

  /** Absolute URL to stream the original uploaded PDF (needs-review preview pane). */
  pdfUrl(expenseId: string): string {
    return this.url(`/expenses/${expenseId}/pdf`);
  }
}
