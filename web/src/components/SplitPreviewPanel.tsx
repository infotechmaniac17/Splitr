"use client";

import { useState } from "react";
import type { AllocationPreviewResponse } from "@splitr/core";
import { Avatar } from "@/components/Avatar";
import { Money } from "@/components/Money";
import type { RememberedMember } from "@/lib/local-store";

/**
 * Live split panel. Fed ONLY by GET /expenses/{id}/allocation-preview (see
 * lib/api's getAllocationPreview) -- every number rendered here comes
 * straight off the response; this component performs no arithmetic on any
 * *_minor field itself (CLAUDE.md invariant #1 / task hard gate).
 */
export function SplitPreviewPanel({
  preview,
  loading,
  error,
  members,
  currency,
}: {
  preview: AllocationPreviewResponse | null;
  loading: boolean;
  error: string | null;
  members: RememberedMember[];
  currency: string;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  function nameFor(userId: string): string {
    return members.find((m) => m.id === userId)?.name ?? userId.slice(0, 8);
  }

  function toggle(userId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  }

  return (
    <div className="rounded-xl border border-gray-200 p-4">
      <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-gray-400">
        Split preview
      </p>

      {loading && <p className="text-sm text-gray-400">Loading split preview…</p>}
      {error && !loading && <p className="text-sm text-red-600">{error}</p>}

      {!loading && !error && preview && (
        <>
          {preview.problems.length > 0 && (
            <ul className="mb-3 flex flex-col gap-1 rounded-lg bg-amber-50 p-2 text-xs text-amber-800">
              {preview.problems.map((p, i) => (
                <li key={i}>{p.message}</li>
              ))}
            </ul>
          )}

          {preview.members.length === 0 ? (
            <p className="text-sm text-gray-400">
              Nothing to preview yet — assign at least one item to see the split.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {preview.members.map((m) => (
                <div key={m.user_id} className="rounded-lg bg-gray-50 px-3 py-2 text-sm">
                  <button
                    type="button"
                    onClick={() => toggle(m.user_id)}
                    className="flex w-full items-center justify-between"
                  >
                    <span className="flex items-center gap-2">
                      <Avatar name={nameFor(m.user_id)} size="sm" />
                      {nameFor(m.user_id)}
                    </span>
                    <span className="flex items-center gap-2">
                      <Money minor={m.total_minor} currency={currency} className="font-semibold" />
                      <span className="text-gray-400">{expanded.has(m.user_id) ? "▾" : "▸"}</span>
                    </span>
                  </button>
                  {expanded.has(m.user_id) && (
                    <dl className="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 border-t border-gray-200 pt-2 text-xs text-gray-500">
                      <dt>Base</dt>
                      <dd className="text-right">
                        <Money minor={m.base_minor} currency={currency} />
                      </dd>
                      <dt>Discount</dt>
                      <dd className="text-right">
                        <Money minor={m.discount_minor} currency={currency} />
                      </dd>
                      <dt>GST</dt>
                      <dd className="text-right">
                        <Money minor={m.gst_minor} currency={currency} />
                      </dd>
                    </dl>
                  )}
                </div>
              ))}

              <div className="mt-1 flex flex-col gap-1 border-t border-gray-200 pt-2 text-xs text-gray-500">
                {preview.subtotal_minor != null && (
                  <div className="flex justify-between">
                    <span>Subtotal</span>
                    <Money minor={preview.subtotal_minor} currency={currency} />
                  </div>
                )}
                {preview.applied_discount_minor != null && preview.applied_discount_minor !== 0 && (
                  <div className="flex justify-between">
                    <span>Discount applied</span>
                    <Money minor={preview.applied_discount_minor} currency={currency} />
                  </div>
                )}
                {preview.exclusive_gst_minor != null && preview.exclusive_gst_minor !== 0 && (
                  <div className="flex justify-between">
                    <span>GST</span>
                    <Money minor={preview.exclusive_gst_minor} currency={currency} />
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      {!loading && !error && !preview && (
        <p className="text-sm text-gray-400">No preview available yet.</p>
      )}
    </div>
  );
}
