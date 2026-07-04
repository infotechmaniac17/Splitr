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
  type AssignmentResponse,
  type AssignmentsPut,
  type ExpenseCreate,
  type ExpenseResponse,
  type GroupBalancesResponse,
  type GroupCreate,
  type GroupMemberAdd,
  type GroupMemberResponse,
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
  accessTokenResponseSchema,
  assignmentResponseSchema,
  expenseResponseSchema,
  groupBalancesResponseSchema,
  groupMemberResponseSchema,
  groupResponseSchema,
  rawExtractionSchema,
  settlementResponseSchema,
  sharesResponseSchema,
  tokenResponseSchema,
  userBalanceResponseSchema,
  userResponseSchema,
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

async function parseJsonOrThrow<T>(res: Response, schema: z.ZodType<T>): Promise<T> {
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
    this.fetchImpl = options.fetchImpl ?? fetch;
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
    schema: z.ZodType<T>,
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

  getGroupBalances(groupId: string): Promise<GroupBalancesResponse> {
    return this.request(
      "GET",
      `/groups/${groupId}/balances`,
      groupBalancesResponseSchema,
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

  createRefund(expenseId: string, payload: RefundCreate): Promise<ExpenseResponse> {
    return this.request(
      "POST",
      `/expenses/${expenseId}/refunds`,
      expenseResponseSchema,
      payload,
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
