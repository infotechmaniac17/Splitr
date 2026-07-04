"use client";

import { use, useEffect, useState } from "react";
import { IdentityGate } from "@/components/IdentityGate";
import { QueuedView } from "@/components/QueuedView";
import { FailedView } from "@/components/FailedView";
import { NeedsReviewView } from "@/components/NeedsReviewView";
import { AssignmentScreen } from "@/components/AssignmentScreen";
import { ConfirmedSummary } from "@/components/ConfirmedSummary";
import { useExpensePolling } from "@/hooks/useExpensePolling";
import { listGroupMembers, type RememberedMember } from "@/lib/local-store";
import { useAuth } from "@/lib/auth";
import type { ExpenseResponse } from "@splitr/core";

function ExpenseDetailContent({ expenseId }: { expenseId: string }) {
  const { user } = useAuth();
  const { expense, error, setExpense } = useExpensePolling(expenseId);
  const [members, setMembers] = useState<RememberedMember[]>([]);

  useEffect(() => {
    if (!expense) return;
    if (expense.group_id) {
      setMembers(listGroupMembers(expense.group_id));
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
      return <NeedsReviewView expense={expense} onCorrected={handleTransition} />;
    case "parsed":
      return (
        <AssignmentScreen
          expense={expense}
          members={members}
          onConfirmed={handleTransition}
        />
      );
    case "confirmed":
      return <ConfirmedSummary expense={expense} />;
    default:
      return null;
  }
}

export default function ExpenseDetailPage({
  params,
}: {
  params: Promise<{ expenseId: string }>;
}) {
  const { expenseId } = use(params);
  return (
    <IdentityGate>
      <ExpenseDetailContent expenseId={expenseId} />
    </IdentityGate>
  );
}
