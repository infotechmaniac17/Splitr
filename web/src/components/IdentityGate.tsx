"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

/** Wrap any page that needs an authenticated user; redirects to /login otherwise. */
export function IdentityGate({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading || !user) {
    return <p className="pt-8 text-center text-sm text-gray-400">Loading…</p>;
  }

  return <>{children}</>;
}
