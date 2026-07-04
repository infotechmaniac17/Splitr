import * as SecureStore from "expo-secure-store";
import { accessTokenResponseSchema, type TokenResponse, type UserResponse } from "@splitr/core";
import { getApiBaseUrl } from "./config";

/**
 * Real session/token store backing mobile auth (replaces the old
 * device-local "who are you" identity in the previous storage.ts).
 *
 * - Refresh token + access token + its expiry + the last-known user are
 *   persisted in expo-secure-store (Keychain/Keystore-backed).
 * - The access token is *also* mirrored in memory (`memoryAccessToken`) so
 *   `SplitrApiClient`'s `getAccessToken` hook — called synchronously on
 *   every request — never has to await SecureStore.
 * - Silent refresh: `scheduleRefresh` arms a timer to call
 *   `POST /auth/refresh` ~60s before the access token (15 min lifetime)
 *   expires, so users doing normal things in the app are never bounced to
 *   the access-token-expired 401 path. This module talks to /auth/refresh
 *   via a bare `fetch` (not the shared SplitrApiClient) specifically to
 *   avoid a circular import with api.ts, which wires its `getAccessToken`
 *   hook back to `getAccessToken` below.
 */

const SESSION_KEY = "splitr.session";

// Refresh this many ms before actual expiry to leave headroom for
// in-flight requests / clock skew.
const REFRESH_MARGIN_MS = 60_000;

export interface Session {
  accessToken: string;
  refreshToken: string;
  accessTokenExpiresAt: number; // epoch ms
  user: UserResponse;
}

let memoryAccessToken: string | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let refreshInFlight: Promise<string | null> | null = null;

// Set by AuthProvider so this module can clear the in-app user state when a
// refresh definitively fails (refresh token expired/invalid/revoked).
let onSessionExpired: (() => void) | null = null;

export function setOnSessionExpired(cb: (() => void) | null): void {
  onSessionExpired = cb;
}

export function getAccessToken(): string | null {
  return memoryAccessToken;
}

function clearRefreshTimer(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh(accessTokenExpiresAt: number): void {
  clearRefreshTimer();
  const delay = Math.max(accessTokenExpiresAt - Date.now() - REFRESH_MARGIN_MS, 0);
  refreshTimer = setTimeout(() => {
    refreshAccessToken().catch(() => {
      // refreshAccessToken already handles/reports failure via
      // onSessionExpired; swallow here so a background timer rejection
      // never surfaces as an unhandled promise rejection.
    });
  }, delay);
}

export async function persistSession(session: Session): Promise<void> {
  memoryAccessToken = session.accessToken;
  await SecureStore.setItemAsync(SESSION_KEY, JSON.stringify(session));
  scheduleRefresh(session.accessTokenExpiresAt);
}

export async function loadSession(): Promise<Session | null> {
  const raw = await SecureStore.getItemAsync(SESSION_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Session;
  } catch {
    return null;
  }
}

export async function clearSession(): Promise<void> {
  clearRefreshTimer();
  memoryAccessToken = null;
  await SecureStore.deleteItemAsync(SESSION_KEY);
}

export function tokenResponseToSession(tokens: TokenResponse): Session {
  return {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    accessTokenExpiresAt: Date.now() + tokens.expires_in * 1000,
    user: tokens.user,
  };
}

/**
 * Exchange the stored refresh token for a new access token. Dedupes
 * concurrent callers (e.g. the silent-refresh timer firing at the same
 * moment a screen's request hits a 401) into a single in-flight request.
 * Returns the new access token, or null if there was no session to refresh.
 * Throws if the backend rejects the refresh token (expired/invalid) --
 * callers should treat that as "log the user out."
 */
export async function refreshAccessToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    const current = await loadSession();
    if (!current) return null;

    try {
      const res = await fetch(`${getApiBaseUrl()}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: current.refreshToken }),
      });
      if (!res.ok) {
        await clearSession();
        onSessionExpired?.();
        return null;
      }
      const body: unknown = await res.json();
      const parsed = accessTokenResponseSchema.parse(body);
      const next: Session = {
        ...current,
        accessToken: parsed.access_token,
        accessTokenExpiresAt: Date.now() + parsed.expires_in * 1000,
      };
      await persistSession(next);
      return next.accessToken;
    } catch {
      await clearSession();
      onSessionExpired?.();
      return null;
    }
  })();

  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

/**
 * Called once at app start. Restores the in-memory access token from
 * SecureStore immediately (so `getAccessToken()` works right away) and
 * arms the silent-refresh timer. If the persisted access token is already
 * past (or within the refresh margin of) expiry, this kicks off an
 * immediate refresh instead of waiting for the timer.
 */
export async function bootstrapSession(): Promise<Session | null> {
  const session = await loadSession();
  if (!session) return null;

  memoryAccessToken = session.accessToken;

  if (session.accessTokenExpiresAt - Date.now() <= REFRESH_MARGIN_MS) {
    const refreshed = await refreshAccessToken();
    if (!refreshed) return null;
    return loadSession();
  }

  scheduleRefresh(session.accessTokenExpiresAt);
  return session;
}
