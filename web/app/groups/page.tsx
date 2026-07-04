"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { IdentityGate } from "@/components/IdentityGate";
import { useAuth } from "@/lib/auth";
import { listRememberedGroups } from "@/lib/local-store";

function GroupsContent() {
  const { user } = useAuth();
  const [groups, setGroups] = useState<{ id: string; name: string }[]>([]);

  useEffect(() => {
    if (user) setGroups(listRememberedGroups(user.id));
  }, [user]);

  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-xl font-bold">Groups</h1>
      {groups.length === 0 && (
        <p className="rounded-lg bg-gray-50 p-4 text-sm text-gray-500">
          No groups yet.
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
  );
}

export default function GroupsPage() {
  return (
    <IdentityGate>
      <GroupsContent />
    </IdentityGate>
  );
}
