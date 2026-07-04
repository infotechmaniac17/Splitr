import { NextRequest, NextResponse } from "next/server";
import { ApiError, SplitrApiClient, type LoginRequest } from "@splitr/core";
import { setRefreshCookie } from "@/lib/auth-cookies";

const backend = new SplitrApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
});

export async function POST(req: NextRequest) {
  let payload: LoginRequest;
  try {
    payload = (await req.json()) as LoginRequest;
  } catch {
    return NextResponse.json({ detail: "Invalid JSON body" }, { status: 400 });
  }

  try {
    const token = await backend.login(payload);
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
    return NextResponse.json({ detail: "Login failed" }, { status: 502 });
  }
}
