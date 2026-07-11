"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  DiscountType,
  formatMoney,
  toMinorUnits,
  type VendorDiscountRuleCreate,
  type VendorDiscountRuleResponse,
} from "@splitr/core";
import { IdentityGate } from "@/components/IdentityGate";
import { Money } from "@/components/Money";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

function RuleForm({
  onSubmit,
  submitting,
  initial,
}: {
  onSubmit: (payload: VendorDiscountRuleCreate) => Promise<void>;
  submitting: boolean;
  initial?: VendorDiscountRuleResponse;
}) {
  const [vendorPattern, setVendorPattern] = useState(initial?.vendor_pattern ?? "");
  const [thresholdInput, setThresholdInput] = useState(
    initial ? formatMoney(initial.min_order_total_minor, "INR", { showSymbol: false }) : "0",
  );
  const [type, setType] = useState<DiscountType>(initial?.discount_type ?? DiscountType.flat);
  const [valueInput, setValueInput] = useState(
    initial
      ? initial.discount_type === DiscountType.flat
        ? formatMoney(initial.discount_value_minor ?? 0, "INR", { showSymbol: false })
        : (initial.discount_percent ?? "")
      : "",
  );
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const payload: VendorDiscountRuleCreate =
        type === DiscountType.flat
          ? {
              vendor_pattern: vendorPattern,
              min_order_total_minor: toMinorUnits(thresholdInput || "0"),
              discount_type: DiscountType.flat,
              discount_value_minor: toMinorUnits(valueInput || "0"),
            }
          : {
              vendor_pattern: vendorPattern,
              min_order_total_minor: toMinorUnits(thresholdInput || "0"),
              discount_type: DiscountType.percent,
              discount_percent: valueInput,
            };
      await onSubmit(payload);
      setVendorPattern("");
      setThresholdInput("0");
      setValueInput("");
    } catch (err) {
      setError(formatApiError(err, "Could not save rule"));
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 rounded-xl border border-gray-200 p-3">
      <input
        value={vendorPattern}
        onChange={(e) => setVendorPattern(e.target.value)}
        placeholder="Vendor name (e.g. Amazon)"
        required
        className="rounded border border-gray-300 px-2 py-1 text-sm"
      />
      <div className="flex gap-2">
        <select
          value={type}
          onChange={(e) => setType(e.target.value as DiscountType)}
          className="rounded border border-gray-300 px-2 py-1 text-sm"
        >
          <option value={DiscountType.flat}>Flat (₹)</option>
          <option value={DiscountType.percent}>Percent (%)</option>
        </select>
        <input
          value={valueInput}
          onChange={(e) => setValueInput(e.target.value)}
          placeholder={type === DiscountType.flat ? "Amount ₹" : "Percent"}
          required
          className="w-28 rounded border border-gray-300 px-2 py-1 text-sm"
        />
      </div>
      <label className="flex flex-col gap-1 text-xs text-gray-500">
        Minimum order total (₹)
        <input
          value={thresholdInput}
          onChange={(e) => setThresholdInput(e.target.value)}
          className="rounded border border-gray-300 px-2 py-1 text-sm"
        />
      </label>
      {error && <p className="text-xs text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={submitting}
        className="rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
      >
        {submitting ? "Saving…" : "Create rule"}
      </button>
    </form>
  );
}

function RuleRow({
  rule,
  canManage,
  onDeactivate,
  onEdit,
}: {
  rule: VendorDiscountRuleResponse;
  canManage: boolean;
  onDeactivate: () => Promise<void>;
  onEdit: (payload: VendorDiscountRuleCreate) => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  async function handleDeactivate() {
    setSubmitting(true);
    setError(null);
    try {
      await onDeactivate();
    } catch (err) {
      setError(formatApiError(err, "Could not deactivate rule"));
    } finally {
      setSubmitting(false);
    }
  }

  if (editing) {
    return (
      <div
        className={`rounded-lg border px-3 py-2 ${rule.active ? "border-gray-200" : "border-gray-100 bg-gray-50"}`}
      >
        <RuleForm
          submitting={submitting}
          initial={rule}
          onSubmit={async (payload) => {
            setSubmitting(true);
            setError(null);
            try {
              await onEdit(payload);
              setEditing(false);
            } catch (err) {
              setError(formatApiError(err, "Could not save rule"));
              throw err;
            } finally {
              setSubmitting(false);
            }
          }}
        />
        <button
          type="button"
          onClick={() => setEditing(false)}
          className="mt-1 text-xs text-gray-500"
        >
          Cancel
        </button>
      </div>
    );
  }

  return (
    <div
      className={`flex items-center justify-between rounded-lg border px-3 py-2 text-sm ${
        rule.active ? "border-gray-200" : "border-gray-100 bg-gray-50 opacity-50"
      }`}
    >
      <div>
        <p className="font-medium">{rule.vendor_pattern}</p>
        <p className="text-xs text-gray-400">
          {rule.discount_type === DiscountType.flat ? (
            <Money minor={rule.discount_value_minor ?? 0} />
          ) : (
            `${rule.discount_percent}%`
          )}{" "}
          off orders of <Money minor={rule.min_order_total_minor} />+
          {!rule.active && " · deactivated"}
        </p>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </div>
      {canManage && (
        <div className="flex gap-3">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="text-xs font-semibold text-brand-700"
          >
            Edit
          </button>
          {rule.active && (
            <button
              type="button"
              onClick={handleDeactivate}
              disabled={submitting}
              className="text-xs font-semibold text-red-600"
            >
              Deactivate
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function VendorDiscountRulesContent({ groupId }: { groupId: string }) {
  const { user } = useAuth();
  const [groupRules, setGroupRules] = useState<VendorDiscountRuleResponse[]>([]);
  const [globalRules, setGlobalRules] = useState<VendorDiscountRuleResponse[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [creatingGlobal, setCreatingGlobal] = useState(false);

  function loadAll() {
    setLoading(true);
    setError(null);
    Promise.all([
      api.listGroupVendorDiscountRules(groupId),
      api.listGlobalVendorDiscountRules(),
      api.getGroupMembers(groupId),
    ])
      .then(([group, global, membersRes]) => {
        setGroupRules(group.rules);
        setGlobalRules(global.rules);
        const me = membersRes.members.find((m) => m.user_id === user?.id);
        setIsAdmin(me?.role === "admin");
      })
      .catch((err) => setError(formatApiError(err, "Could not load discount rules")))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (user) loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, user]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Vendor discount rules</h1>
        <Link href={`/groups/${groupId}`} className="text-xs font-medium text-brand-700">
          Back to group
        </Link>
      </div>

      {loading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}

      {!loading && !error && (
        <>
          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-semibold text-gray-500">
              This group{!isAdmin && " (view only — admins can manage)"}
            </h2>
            {groupRules.length === 0 && (
              <p className="text-sm text-gray-400">No rules for this group yet.</p>
            )}
            {groupRules.map((rule) => (
              <RuleRow
                key={rule.id}
                rule={rule}
                canManage={isAdmin}
                onDeactivate={async () => {
                  await api.deactivateGroupVendorDiscountRule(groupId, rule.id);
                  loadAll();
                }}
                onEdit={async (payload) => {
                  await api.updateGroupVendorDiscountRule(groupId, rule.id, payload);
                  loadAll();
                }}
              />
            ))}
            {isAdmin && (
              <RuleForm
                submitting={creatingGroup}
                onSubmit={async (payload) => {
                  setCreatingGroup(true);
                  try {
                    await api.createGroupVendorDiscountRule(groupId, payload);
                    loadAll();
                  } finally {
                    setCreatingGroup(false);
                  }
                }}
              />
            )}
          </section>

          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-semibold text-gray-500">
              Your global rules (any of your groups)
            </h2>
            {globalRules.length === 0 && (
              <p className="text-sm text-gray-400">You have no global rules yet.</p>
            )}
            {globalRules.map((rule) => (
              <RuleRow
                key={rule.id}
                rule={rule}
                canManage
                onDeactivate={async () => {
                  await api.deactivateGlobalVendorDiscountRule(rule.id);
                  loadAll();
                }}
                onEdit={async (payload) => {
                  await api.updateGlobalVendorDiscountRule(rule.id, payload);
                  loadAll();
                }}
              />
            ))}
            <RuleForm
              submitting={creatingGlobal}
              onSubmit={async (payload) => {
                setCreatingGlobal(true);
                try {
                  await api.createGlobalVendorDiscountRule({ ...payload, group_id: null });
                  loadAll();
                } finally {
                  setCreatingGlobal(false);
                }
              }}
            />
          </section>
        </>
      )}
    </div>
  );
}

export default function VendorDiscountRulesPage({
  params,
}: {
  params: { groupId: string };
}) {
  const { groupId } = params;
  return (
    <IdentityGate>
      <VendorDiscountRulesContent groupId={groupId} />
    </IdentityGate>
  );
}
