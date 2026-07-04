import { ApiError, SplitrApiClient } from "@splitr/core";
import { getAccessToken, refreshAccessToken } from "@/lib/token-store";

/**
 * Single shared API client instance for the web app. Base URL comes from
 * NEXT_PUBLIC_API_BASE_URL (must be exposed to the browser since the
 * assignment/upload/polling screens are all client components) — see
 * web/.env.example. Defaults to the local backend dev server.
 *
 * `getAccessToken` is wired to the in-memory token store (lib/token-store.ts)
 * so every request automatically gets `Authorization: Bearer <token>` once
 * the user is signed in (see lib/auth.tsx).
 */
const rawApi = new SplitrApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
  getAccessToken,
});

// Methods that are synchronous and must NOT be wrapped in the async
// auth-retry proxy below (it would turn their return value into a Promise).
const SYNC_METHODS = new Set<string | symbol>(["pdfUrl"]);

/**
 * Wraps every async SplitrApiClient method so a single 401 (expired/invalid
 * access token — e.g. the 15-minute window lapsed while a tab was idle)
 * triggers one silent-refresh-and-retry before surfacing the error. This is
 * the reactive complement to token-store.ts's proactive scheduled refresh.
 *
 * 403 (authenticated but not authorized — wrong actor / not a group member,
 * per the money-path actor-authorization rules) is deliberately NOT retried
 * here: a fresh token can't fix "you aren't a member of this group". Callers
 * should check `err.status === 403` themselves and show a clear message
 * (see formatApiError below).
 */
function wrapWithAuthRetry(client: SplitrApiClient): SplitrApiClient {
  return new Proxy(client, {
    get(target, prop, receiver) {
      const value = Reflect.get(target, prop, receiver);
      if (typeof value !== "function" || SYNC_METHODS.has(prop)) {
        return typeof value === "function" ? value.bind(target) : value;
      }
      const fn = value as (...args: unknown[]) => Promise<unknown>;
      return async (...args: unknown[]) => {
        try {
          return await fn.apply(target, args);
        } catch (err) {
          if (err instanceof ApiError && err.status === 401) {
            const refreshed = await refreshAccessToken();
            if (refreshed) {
              return await fn.apply(target, args);
            }
          }
          throw err;
        }
      };
    },
  }) as SplitrApiClient;
}

export const api = wrapWithAuthRetry(rawApi);

/**
 * Turns an ApiError (or arbitrary thrown value) from a money-path call into
 * a message safe to render directly, distinguishing "your session is gone"
 * (401) from "you're authenticated but not allowed to do this" (403 — e.g.
 * not a member of the group) from everything else.
 */
export function formatApiError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return "Your session has expired. Please sign in again.";
    }
    if (err.status === 403) {
      return typeof err.detail === "string"
        ? err.detail
        : "You don't have permission to do this — you may not be a member of this group.";
    }
    if (typeof err.detail === "string") return err.detail;
  }
  return err instanceof Error ? err.message : fallback;
}
