import React, {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { ApiError, type UserResponse } from "@splitr/core";
import { apiClient } from "./api";
import {
  bootstrapSession,
  clearSession,
  loadSession,
  persistSession,
  setOnSessionExpired,
  tokenResponseToSession,
} from "./session";
import { registerForPushNotificationsAsync } from "./notifications";

/**
 * Real email+password auth, backed by the backend's JWT auth surface
 * (backend/app/domain/auth.py, backend/app/api/auth.py):
 *   - register(): POST /auth/register (auto-logs in, returns TokenResponse)
 *   - login():    POST /auth/login
 *   - silent refresh: session.ts arms a timer that calls POST /auth/refresh
 *     ~60s before the 15-minute access token expires, and api.ts retries
 *     any request that still gets a 401 with one refresh-and-retry.
 *   - logout(): clears the persisted session (expo-secure-store).
 *
 * Replaces the previous device-local "who are you" stopgap (device-only
 * identity via POST /users, login-by-pasting-a-user-id) now that the
 * backend has real auth.
 */

interface AuthContextValue {
  user: UserResponse | null;
  isLoading: boolean;
  register: (params: {
    name: string;
    email: string;
    password: string;
    phone?: string;
  }) => Promise<void>;
  login: (params: { email: string; password: string }) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function friendlyApiError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return "Your session has expired. Please log in again.";
    }
    if (typeof err.detail === "string") return err.detail;
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return "Something went wrong. Please try again.";
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    // Wired so a failed silent refresh (refresh token expired/revoked, or a
    // reactive 401-retry that also fails) clears the in-app user state from
    // wherever it happens, not just from an explicit logout() call. `app/
    // index.tsx` redirects to the login stack whenever `user` is null.
    setOnSessionExpired(() => setUser(null));

    let cancelled = false;
    (async () => {
      const restored = await bootstrapSession();
      if (cancelled) return;
      if (!restored) {
        setUser(null);
        setIsLoading(false);
        return;
      }
      // Optimistically show the last-known user immediately, then confirm
      // against /auth/me in the background (catches revoked/deleted users
      // without blocking app startup on a network round trip).
      setUser(restored.user);
      setIsLoading(false);
      try {
        const fresh = await apiClient.getCurrentUser();
        if (!cancelled) setUser(fresh);
      } catch {
        // If the token is genuinely invalid, api.ts's 401 retry + session's
        // onSessionExpired callback will already have cleared the user;
        // otherwise this was a transient network error and the optimistic
        // user above is left in place.
      }
    })();

    return () => {
      cancelled = true;
      setOnSessionExpired(null);
    };
  }, []);

  useEffect(() => {
    if (user) {
      // Fire-and-forget: wire push token registration once we have an
      // identity. Backend endpoint doesn't exist yet either — see
      // notifications.ts — so this is safe to fail silently.
      registerForPushNotificationsAsync(user.id).catch(() => {});
    }
  }, [user]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isLoading,
      async register({ name, email, password, phone }) {
        const tokens = await apiClient.register({
          name,
          email,
          password,
          phone: phone || null,
          avatar_url: null,
          default_currency: "INR",
        });
        await persistSession(tokenResponseToSession(tokens));
        setUser(tokens.user);
      },
      async login({ email, password }) {
        const tokens = await apiClient.login({ email, password });
        await persistSession(tokenResponseToSession(tokens));
        setUser(tokens.user);
      },
      async logout() {
        await clearSession();
        setUser(null);
      },
    }),
    [user, isLoading],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}

// Re-exported for anywhere that needs to peek at a persisted session
// without subscribing to the React context (currently unused, kept for
// parity with session.ts's public surface).
export { loadSession };
