"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

const NAV_ITEMS = [
  { href: "/", label: "Home" },
  { href: "/groups", label: "Groups" },
  { href: "/expenses/upload", label: "Upload" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading, signOut } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isAuthRoute = pathname === "/login";

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col bg-white shadow-sm sm:max-w-lg">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-gray-200 bg-white px-4 py-3">
        <Link href="/" className="text-lg font-semibold text-brand-700">
          Splitr
        </Link>
        {!loading && user && (
          <button
            type="button"
            onClick={() => {
              signOut();
              router.push("/login");
            }}
            className="flex items-center gap-2 rounded-full bg-gray-100 px-3 py-1 text-sm text-gray-700"
          >
            <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-brand-500 text-xs font-semibold text-white">
              {user.name.slice(0, 1).toUpperCase()}
            </span>
            {user.name.split(" ")[0]}
          </button>
        )}
      </header>

      <main className="flex-1 px-4 pb-20 pt-4">{children}</main>

      {!isAuthRoute && (
        <nav className="sticky bottom-0 z-10 grid grid-cols-3 border-t border-gray-200 bg-white text-center text-xs">
          {NAV_ITEMS.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex flex-col items-center gap-1 py-3 ${
                  active ? "font-semibold text-brand-700" : "text-gray-500"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      )}
    </div>
  );
}
