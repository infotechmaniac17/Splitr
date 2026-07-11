import type { NextResponse } from "next/server";

/**
 * Server-only cookie helpers used by app/api/auth/*\/route.ts. The refresh
 * token NEVER goes to client-side JS — it's set here as httpOnly so an XSS
 * bug can't exfiltrate it, and scoped to /api/auth so the browser doesn't
 * even send it on ordinary page navigations.
 */
export const REFRESH_COOKIE_NAME = "splitr_refresh_token";

// Mirrors backend REFRESH_TOKEN_EXPIRE_DAYS (backend/app/domain/auth.py).
const REFRESH_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60;

export function setRefreshCookie(
  res: NextResponse,
  refreshToken: string,
): void {
  res.cookies.set(REFRESH_COOKIE_NAME, refreshToken, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/api/auth",
    maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
  });
}

export function clearRefreshCookie(res: NextResponse): void {
  res.cookies.set(REFRESH_COOKIE_NAME, "", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/api/auth",
    maxAge: 0,
  });
}
