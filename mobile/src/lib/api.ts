import { ApiError, SplitrApiClient } from "@splitr/core";
import { getApiBaseUrl } from "./config";
import { getAccessToken, refreshAccessToken } from "./session";

/**
 * Single shared API client instance, reusing @splitr/core's SplitrApiClient
 * (same client web/ uses) rather than hand-rolling fetch calls in mobile/.
 * `getAccessToken` is wired to session.ts's in-memory token mirror so every
 * request automatically carries `Authorization: Bearer <access_token>`.
 */
const baseClient = new SplitrApiClient({
  baseUrl: getApiBaseUrl(),
  getAccessToken,
});

/**
 * Wraps every SplitrApiClient method so a 401 (access token expired/invalid
 * -- see backend/app/api/deps.py:get_current_user) triggers exactly one
 * silent-refresh-and-retry before giving up. This is orchestration around
 * the shared client, not a fork of its request/parsing logic, so it lives
 * here rather than in packages/core.
 *
 * 403s (authenticated but not authorized for this actor/group -- see
 * app/api/expenses.py's _assert_actor_authorized_for_* helpers) are NOT
 * retried here; they pass through as-is so callers can show the backend's
 * human-readable detail message (see friendlyApiError in lib/auth.tsx).
 */
function withAuthRetry<T extends object>(client: T): T {
  return new Proxy(client, {
    get(target, prop, receiver) {
      const original = Reflect.get(target, prop, receiver) as (
        ...a: unknown[]
      ) => unknown;
      if (typeof original !== "function") return original;

      // Some client methods (e.g. pdfUrl) are synchronous string builders,
      // not network calls — only wrap ones that return a Promise so we
      // never turn a sync return value into an async one.
      return function (this: unknown, ...args: unknown[]) {
        const result = original.apply(target, args);
        if (!(result instanceof Promise)) return result;

        return result.catch(async (err: unknown) => {
          if (err instanceof ApiError && err.status === 401) {
            const refreshed = await refreshAccessToken().catch(() => null);
            if (refreshed) {
              return original.apply(target, args);
            }
          }
          throw err;
        });
      };
    },
  }) as T;
}

export const apiClient = withAuthRetry(baseClient);
