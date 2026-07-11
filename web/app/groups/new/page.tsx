"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { IdentityGate } from "@/components/IdentityGate";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { rememberGroup } from "@/lib/local-store";

function NewGroupContent() {
  const { user } = useAuth();
  const router = useRouter();
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!user) return;
    setSubmitting(true);
    setError(null);
    try {
      const group = await api.createGroup({
        name,
        created_by: user.id,
        simplify_debts: true,
      });
      rememberGroup(user.id, { id: group.id, name: group.name });
      router.push(`/groups/${group.id}`);
    } catch (err) {
      setError(formatApiError(err, "Failed to create group"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <h1 className="text-xl font-bold">New group</h1>
      <label className="flex flex-col gap-1 text-sm font-medium text-gray-700">
        Group name
        <input
          required
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Goa Trip"
          className="rounded-lg border border-gray-300 px-3 py-2 text-base"
        />
      </label>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={submitting || !name.trim()}
        className="rounded-lg bg-brand-600 px-4 py-3 font-semibold text-white disabled:opacity-50"
      >
        {submitting ? "Creating…" : "Create group"}
      </button>
    </form>
  );
}

export default function NewGroupPage() {
  return (
    <IdentityGate>
      <NewGroupContent />
    </IdentityGate>
  );
}
