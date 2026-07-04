"use client";

/**
 * In-memory access-token store for the web app, plus the silent-refresh
 * mechanics. Deliberately NOT localStorage/sessionStorage — an access token
 * sitting in Web Storage is readable by any injected script (XSS), whereas
 * an in-memory variable disappears on tab close/reload and is never
 * serialized anywhere. The refresh token never reaches this module (or any
 * client-side JS) at all: it lives only in an httpOnly cookie set by
 * app/api/auth/{login,register,refresh}/route.ts, which the browser sends
 * automatically to `/api/auth/refresh` — see refreshAccessToken() below.
 *
 * Tradeoff: because the access token is in-memory only, a hard page reload
 * loses it, so every fresh page load must call refreshAccessToken() once
 * (see lib/auth.tsx's AuthProvider mount effect) to mint a new one from the
 * refresh cookie before the app can consider the user signed in. That round
 * trip is the price of not touching localStorage for the token itself.
 */

type Listener = () => void;

let accessToken: string | null = null;
let expiresAt: number | null = null; // epoch ms
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let refreshInFlight: Promise<boolean> | null = null;
const listeners = new Set<Listener>();

// Refresh a bit before actual expiry so in-flight requests don't race the
// clock; access tokens are short-lived (15 min per backend/app/domain/auth.py)
// so a 60s skew is generous without over-refreshing.
const REFRESH_SKEW_MS = 60_000;
const MIN_REFRESH_DELAY_MS = 5_000;

export function getAccessToken(): string | null {
  return accessToken;
}

export function subscribeToken(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(): void {
  listeners.forEach((l) => l());
}

/** Called after register/login/refresh succeed with the new access token. */
export function setSession(token: string, expiresInSeconds: number): void {
  accessToken = token;
  expiresAt = Date.now() + expiresInSeconds * 1000;
  scheduleRefresh();
  notify();
}

/** Called on sign-out, or when a refresh attempt definitively fails. */
export function clearSession(): void {
  accessToken = null;
  expiresAt = null;
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = null;
  notify();
}

function scheduleRefresh(): void {
  if (refreshTimer) clearTimeout(refreshTimer);
  if (!expiresAt) return;
  const delay = Math.max(expiresAt - Date.now() - REFRESH_SKEW_MS, MIN_REFRESH_DELAY_MS);
  refreshTimer = setTimeout(() => {
    void refreshAccessToken();
  }, delay);
}

/**
 * Silent refresh: hits our own Next.js route handler (not the backend
 * directly) with `credentials: "same-origin"` so the httpOnly refresh-token
 * cookie — which this module can never read — is attached automatically.
 * The route handler (app/api/auth/refresh/route.ts) forwards it to
 * POST /auth/refresh server-side and hands back only a fresh access token.
 *
 * Concurrent callers (e.g. several API calls all hitting a 401 at once)
 * share one in-flight request instead of each firing their own refresh.
 */
export function refreshAccessToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const res = await fetch("/api/auth/refresh", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!res.ok) {
        clearSession();
        return false;
      }
      const body = (await res.json()) as { access_token: string; expires_in: number };
      setSession(body.access_token, body.expires_in);
      return true;
    } catch {
      clearSession();
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}
