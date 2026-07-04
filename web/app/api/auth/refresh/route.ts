import { NextRequest, NextResponse } from "next/server";
import { ApiError, SplitrApiClient } from "@splitr/core";
import { REFRESH_COOKIE_NAME, clearRefreshCookie } from "@/lib/auth-cookies";

const backend = new SplitrApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
});

export async function POST(req: NextRequest) {
  const refreshToken = req.cookies.get(REFRESH_COOKIE_NAME)?.value;
  if (!refreshToken) {
    return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  }

  try {
    // No rotation on this backend pass (see backend/app/domain/auth.py) --
    // the same refresh token is reused until it expires or the user logs in
    // again, so we don't need to re-set the cookie here.
    const token = await backend.refresh({ refresh_token: refreshToken });
    return NextResponse.json(token);
  } catch (err) {
    const status = err instanceof ApiError ? err.status : 502;
    const detail = err instanceof ApiError ? err.detail : "Session expired";
    const res = NextResponse.json({ detail }, { status });
    if (status === 401) {
      // Refresh token itself is invalid/expired -- stop sending it.
      clearRefreshCookie(res);
    }
    return res;
  }
}
