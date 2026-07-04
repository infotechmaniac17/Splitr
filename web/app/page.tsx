"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { UserBalanceResponse } from "@splitr/core";
import { IdentityGate } from "@/components/IdentityGate";
import { Money } from "@/components/Money";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";
import { listRememberedGroups } from "@/lib/local-store";

type Mode = "personal" | "groups";

function DashboardContent() {
  const { user } = useAuth();
  const [mode, setMode] = useState<Mode>("personal");
  const [balance, setBalance] = useState<UserBalanceResponse | null>(null);
  const [groups, setGroups] = useState<{ id: string; name: string }[]>([]);

  useEffect(() => {
    if (!user) return;
    api.getUserBalance(user.id).then(setBalance).catch(() => setBalance(null));
    setGroups(listRememberedGroups(user.id));
  }, [user]);

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 text-sm font-medium">
        {(["personal", "groups"] as Mode[]).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`rounded-md py-2 capitalize transition ${
              mode === m ? "bg-white shadow" : "text-gray-500"
            }`}
          >
            {m}
          </button>
        ))}
      </div>

      {mode === "personal" ? (
        <div className="flex flex-col gap-4">
          <div className="rounded-xl border border-gray-200 p-5">
            <p className="text-sm text-gray-500">Your net balance</p>
            {balance ? (
              <p
                className={`mt-1 text-3xl font-bold ${
                  balance.net_balance_minor >= 0 ? "text-emerald-600" : "text-red-600"
                }`}
              >
                <Money minor={balance.net_balance_minor} showPositiveSign />
              </p>
            ) : (
              <p className="mt-1 text-3xl font-bold text-gray-300">…</p>
            )}
            <p className="mt-1 text-xs text-gray-400">
              Positive = you are owed money. Negative = you owe money.
            </p>
          </div>

          <Link
            href="/expenses/upload"
            className="rounded-xl border-2 border-dashed border-brand-300 p-5 text-center text-sm font-medium text-brand-700"
          >
            + Upload an invoice
          </Link>

          <div className="rounded-lg bg-gray-50 p-3 text-xs text-gray-500">
            Your Splitr ID (share this so others can add you to a group):
            <div className="mt-1 select-all break-all rounded bg-white p-2 font-mono text-gray-700">
              {user?.id}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.length === 0 && (
            <p className="rounded-lg bg-gray-50 p-4 text-sm text-gray-500">
              No groups yet. Create one to start splitting bills together.
            </p>
          )}
          {groups.map((g) => (
            <Link
              key={g.id}
              href={`/groups/${g.id}`}
              className="flex items-center justify-between rounded-xl border border-gray-200 p-4"
            >
              <span className="font-medium">{g.name}</span>
              <span className="text-gray-400">›</span>
            </Link>
          ))}
          <Link
            href="/groups/new"
            className="rounded-xl border-2 border-dashed border-brand-300 p-4 text-center text-sm font-medium text-brand-700"
          >
            + New group
          </Link>
        </div>
      )}
    </div>
  );
}

export default function DashboardPage() {
  return (
    <IdentityGate>
      <DashboardContent />
    </IdentityGate>
  );
}
