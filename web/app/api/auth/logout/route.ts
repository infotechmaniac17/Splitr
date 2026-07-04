import { NextResponse } from "next/server";
import { clearRefreshCookie } from "@/lib/auth-cookies";

/**
 * There's no server-side revocation store yet (refresh tokens are stateless
 * JWTs per backend/app/domain/auth.py's docstring), so "logout" here just
 * means: stop sending the refresh cookie and drop the in-memory access
 * token client-side (see lib/token-store.ts's clearSession, called by
 * lib/auth.tsx before this request is even sent).
 */
export async function POST() {
  const res = NextResponse.json({ ok: true });
  clearRefreshCookie(res);
  return res;
}
