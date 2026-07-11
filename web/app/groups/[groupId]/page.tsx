"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { GroupBalancesResponse, GroupMemberInfo, GroupResponse } from "@splitr/core";
import { IdentityGate } from "@/components/IdentityGate";
import { Money } from "@/components/Money";
import { Avatar } from "@/components/Avatar";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { listRememberedExpenses } from "@/lib/local-store";

function GroupDetailContent({ groupId }: { groupId: string }) {
  const { user } = useAuth();
  const [group, setGroup] = useState<GroupResponse | null>(null);
  const [balances, setBalances] = useState<GroupBalancesResponse | null>(null);
  const [members, setMembers] = useState<GroupMemberInfo[]>([]);
  const [expenses, setExpenses] = useState<{ id: string }[]>([]);
  const [memberIdInput, setMemberIdInput] = useState("");
  const [addingMember, setAddingMember] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refetchMembers() {
    api
      .getGroupMembers(groupId)
      .then((res) => setMembers(res.members))
      .catch(() => setMembers([]));
  }

  useEffect(() => {
    api.getGroup(groupId).then(setGroup).catch(() => setGroup(null));
    api.getGroupBalances(groupId).then(setBalances).catch(() => setBalances(null));
    refetchMembers();
    if (user) {
      setExpenses(
        listRememberedExpenses(user.id).filter((e) => e.groupId === groupId),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, user]);

  function nameFor(userId: string): string {
    return members.find((m) => m.user_id === userId)?.name ?? userId.slice(0, 8);
  }

  async function handleAddMember(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setAddingMember(true);
    try {
      await api.addGroupMember(groupId, { user_id: memberIdInput.trim() });
      refetchMembers();
      setMemberIdInput("");
    } catch (err) {
      setError(formatApiError(err, "Could not add member"));
    } finally {
      setAddingMember(false);
    }
  }

  if (!group) return <p className="pt-8 text-center text-sm text-gray-400">Loading…</p>;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-bold">{group.name}</h1>
        <p className="text-xs text-gray-400">
          {group.simplify_debts ? "Debt simplification on" : "Debt simplification off"}
        </p>
      </div>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-500">Balances</h2>
        {balances && balances.balances.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {balances.balances.map((b, i) => (
              <li
                key={i}
                className="flex items-center justify-between rounded-lg border border-gray-200 px-3 py-2 text-sm"
              >
                <span>
                  <strong>{nameFor(b.debtor_id)}</strong> owes{" "}
                  <strong>{nameFor(b.creditor_id)}</strong>
                </span>
                <Money minor={b.net_amount_minor} className="font-semibold" />
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-gray-400">All settled up.</p>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-500">Members</h2>
        <div className="flex flex-wrap items-center gap-2">
          {members.map((m) => (
            <span key={m.user_id} className="flex items-center gap-1 text-xs">
              <Avatar name={m.name} size="sm" />
              {m.name}
            </span>
          ))}
        </div>
        <form onSubmit={handleAddMember} className="mt-3 flex gap-2">
          <input
            value={memberIdInput}
            onChange={(e) => setMemberIdInput(e.target.value)}
            placeholder="Member's Splitr user ID"
            className="flex-1 rounded-lg border border-gray-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={addingMember || !memberIdInput.trim()}
            className="rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            Add
          </button>
        </form>
        {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
        <p className="mt-1 text-xs text-gray-400">
          Ask a member to share their user ID from their profile chip.
        </p>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-gray-500">Expenses</h2>
        {expenses.length === 0 && (
          <p className="text-sm text-gray-400">No expenses yet in this browser.</p>
        )}
        {expenses.map((e) => (
          <Link
            key={e.id}
            href={`/expenses/${e.id}`}
            className="rounded-lg border border-gray-200 px-3 py-2 text-sm"
          >
            {e.id}
          </Link>
        ))}
        <div className="mt-2 grid grid-cols-2 gap-2">
          <Link
            href={`/expenses/upload?groupId=${groupId}`}
            className="rounded-lg border-2 border-dashed border-brand-300 py-3 text-center text-sm font-medium text-brand-700"
          >
            Upload invoice
          </Link>
          <Link
            href={`/expenses/manual?groupId=${groupId}`}
            className="rounded-lg border-2 border-dashed border-gray-300 py-3 text-center text-sm font-medium text-gray-700"
          >
            Manual entry
          </Link>
        </div>
      </section>
    </div>
  );
}

export default function GroupDetailPage({
  params,
}: {
  params: { groupId: string };
}) {
  const { groupId } = params;
  return (
    <IdentityGate>
      <GroupDetailContent groupId={groupId} />
    </IdentityGate>
  );
}
