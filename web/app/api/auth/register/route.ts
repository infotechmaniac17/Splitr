import { NextRequest, NextResponse } from "next/server";
import { ApiError, SplitrApiClient, type RegisterRequest } from "@splitr/core";
import { setRefreshCookie } from "@/lib/auth-cookies";

// Server-side only: this instance never gets a getAccessToken hook and never
// runs in the browser, so it's fine for it to talk to the backend directly.
const backend = new SplitrApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
});

export async function POST(req: NextRequest) {
  let payload: RegisterRequest;
  try {
    payload = (await req.json()) as RegisterRequest;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  try {
    const token = await backend.register(payload);
    // Return only the access token + user to the browser; the refresh token
    // goes straight into an httpOnly cookie and is never exposed to JS.
    const res = NextResponse.json({
      access_token: token.access_token,
      expires_in: token.expires_in,
      user: token.user,
    });
    setRefreshCookie(res, token.refresh_token);
    return res;
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Registration failed" }, { status: 502 });
  }
}
