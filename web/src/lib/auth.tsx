"use client";

/**
 * Real authentication for the web app, replacing the old client-side-only
 * "who are you" localStorage picker (see git history of this file / the
 * removed web/src/lib/current-user.tsx) now that the backend has real auth
 * (backend/app/api/auth.py, backend/app/domain/auth.py).
 *
 * Session model:
 *  - Access token: kept in memory only (lib/token-store.ts), attached to
 *    every SplitrApiClient request via the `getAccessToken` hook (lib/api.ts).
 *    Never written to localStorage/sessionStorage (XSS exposure) — the
 *    tradeoff is a page reload always needs one silent-refresh round trip
 *    before the app knows who's signed in (see the mount effect below).
 *  - Refresh token: httpOnly cookie set by app/api/auth/{login,register,
 *    refresh}/route.ts. This module (and all client-side JS) never sees its
 *    value — it's forwarded to POST /auth/refresh server-side only.
 *  - Silent refresh: token-store.ts proactively schedules a refresh ~60s
 *    before the 15-minute access token expires; lib/api.ts additionally
 *    retries once on a reactive 401. Either path calls refreshAccessToken(),
 *    which updates the shared token store and (via subscribeToken below)
 *    this context's `user`/session state.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  ApiError,
  type LoginRequest,
  type RegisterRequest,
  type UserResponse,
} from "@splitr/core";
import { api } from "@/lib/api";
import {
  clearSession,
  getAccessToken,
  refreshAccessToken,
  setSession,
  subscribeToken,
} from "@/lib/token-store";

interface AuthTokenResponse {
  access_token: string;
  expires_in: number;
  user: UserResponse;
}

interface AuthContextValue {
  user: UserResponse | null;
  loading: boolean;
  login: (payload: LoginRequest) => Promise<void>;
  register: (payload: RegisterRequest) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function postAuthRoute(
  path: string,
  body: unknown,
): Promise<AuthTokenResponse> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await res.text();
  const json: unknown = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    const detail =
      json && typeof json === "object" && "detail" in json
        ? (json as { detail: unknown }).detail
        : json;
    throw new ApiError(res.status, detail);
  }
  return json as AuthTokenResponse;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [loading, setLoading] = useState(true);

  // If a background silent refresh (proactive timer, or a reactive 401
  // retry from lib/api.ts) ever definitively fails, the token store clears
  // itself -- mirror that here so IdentityGate redirects to /login instead
  // of the UI silently going stale.
  useEffect(() => {
    return subscribeToken(() => {
      if (getAccessToken() === null) {
        setUser((prev) => (prev ? null : prev));
      }
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const ok = await refreshAccessToken();
      if (cancelled) return;
      if (ok) {
        try {
          const me = await api.getCurrentUser();
          if (!cancelled) setUser(me);
        } catch {
          if (!cancelled) setUser(null);
        }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (payload: LoginRequest) => {
    const data = await postAuthRoute("/api/auth/login", payload);
    setSession(data.access_token, data.expires_in);
    setUser(data.user);
  }, []);

  const register = useCallback(async (payload: RegisterRequest) => {
    const data = await postAuthRoute("/api/auth/register", payload);
    setSession(data.access_token, data.expires_in);
    setUser(data.user);
  }, []);

  const signOut = useCallback(async () => {
    clearSession();
    setUser(null);
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      // Best-effort -- local session is already cleared either way.
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, register, signOut }),
    [user, loading, login, register, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}
