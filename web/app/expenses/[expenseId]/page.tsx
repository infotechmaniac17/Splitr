"use client";

import { useEffect, useState } from "react";
import { IdentityGate } from "@/components/IdentityGate";
import { QueuedView } from "@/components/QueuedView";
import { FailedView } from "@/components/FailedView";
import { NeedsReviewView } from "@/components/NeedsReviewView";
import { AssignmentScreen } from "@/components/AssignmentScreen";
import { ConfirmedSummary } from "@/components/ConfirmedSummary";
import { useExpensePolling } from "@/hooks/useExpensePolling";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { RememberedMember } from "@/lib/local-store";
import type { ExpenseResponse } from "@splitr/core";

function ExpenseDetailContent({ expenseId }: { expenseId: string }) {
  const { user } = useAuth();
  const { expense, error, setExpense } = useExpensePolling(expenseId);
  const [members, setMembers] = useState<RememberedMember[]>([]);

  useEffect(() => {
    if (!expense) return;
    if (expense.group_id) {
      api
        .getGroupMembers(expense.group_id)
        .then((res) =>
          setMembers(res.members.map((m) => ({ id: m.user_id, name: m.name }))),
        )
        .catch(() => setMembers([]));
    } else if (user) {
      setMembers([{ id: user.id, name: user.name }]);
    }
  }, [expense, user]);

  function handleTransition(updated: ExpenseResponse) {
    setExpense(updated);
  }

  if (error && !expense) {
    return <p className="pt-8 text-center text-sm text-red-600">{error}</p>;
  }
  if (!expense) {
    return <QueuedView />;
  }

  switch (expense.parse_status) {
    case "queued":
      return <QueuedView />;
    case "failed":
      return <FailedView expense={expense} />;
    case "needs_review":
      return (
        <NeedsReviewView expense={expense} onCorrected={handleTransition} />
      );
    case "parsed":
      return (
        <AssignmentScreen
          expense={expense}
          members={members}
          onConfirmed={handleTransition}
          onExpenseUpdated={handleTransition}
        />
      );
    case "confirmed":
      return <ConfirmedSummary expense={expense} members={members} />;
    default:
      return null;
  }
}

export default function ExpenseDetailPage({
  params,
}: {
  params: { expenseId: string };
}) {
  const { expenseId } = params;
  return (
    <IdentityGate>
      <ExpenseDetailContent expenseId={expenseId} />
    </IdentityGate>
  );
}
