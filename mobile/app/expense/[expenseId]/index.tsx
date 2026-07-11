import React, { useCallback, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useFocusEffect, router, useLocalSearchParams } from "expo-router";
import {
  formatMoney,
  lineItemKindLabels,
  type ExpenseResponse,
  type SharesResponse,
} from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { StatusBadge } from "@/components/StatusBadge";
import { apiClient } from "@/lib/api";
import { friendlyApiError } from "@/lib/auth";
import { colors, radius, spacing } from "@/lib/theme";

export default function ExpenseDetailScreen() {
  const { expenseId } = useLocalSearchParams<{ expenseId: string }>();
  const [expense, setExpense] = useState<ExpenseResponse | null>(null);
  const [shares, setShares] = useState<SharesResponse | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    if (!expenseId) return;
    const e = await apiClient.getExpense(expenseId);
    setExpense(e);
    if (e.parse_status === "parsed" || e.parse_status === "confirmed") {
      try {
        const s = await apiClient.getShares(expenseId);
        setShares(s);
      } catch {
        setShares(null);
      }
    } else {
      setShares(null);
    }
  }, [expenseId]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  async function onConfirm() {
    if (!expenseId) return;
    setConfirmError(null);
    setConfirming(true);
    try {
      const confirmed = await apiClient.confirmExpense(expenseId);
      setExpense(confirmed);
    } catch (err) {
      setConfirmError(friendlyApiError(err));
    } finally {
      setConfirming(false);
    }
  }

  if (!expense) {
    return (
      <Screen>
        <Text style={styles.muted}>Loading…</Text>
      </Screen>
    );
  }

  return (
    <Screen>
      <View style={styles.headerRow}>
        <Text style={styles.title}>{expense.vendor || "Expense"}</Text>
        <StatusBadge status={expense.parse_status} />
      </View>
      <Text style={styles.total}>
        {formatMoney(expense.total_minor, expense.currency)}
      </Text>

      {expense.parse_status === "needs_review" && (
        <View style={styles.calloutWarning}>
          <Text style={styles.calloutText}>
            Extraction couldn't be validated automatically. Review and correct
            the line items before splitting.
          </Text>
          <Button
            title="Review & correct"
            onPress={() => router.push(`/expense/${expense.id}/review`)}
            style={{ marginTop: spacing.sm }}
          />
        </View>
      )}

      {expense.parse_status === "failed" && (
        <View style={styles.calloutDanger}>
          <Text style={styles.calloutText}>
            This receipt couldn't be parsed at all. Enter it manually instead
            (Quick Manual Entry).
          </Text>
          <Button
            title="Enter manually"
            onPress={() => router.push("/expense/new")}
            style={{ marginTop: spacing.sm }}
          />
        </View>
      )}

      {expense.parse_status === "queued" && (
        <Text style={styles.muted}>
          Still processing — pull to refresh in a moment.
        </Text>
      )}

      {expense.parse_status === "parsed" && (
        <View style={styles.calloutInfo}>
          <Text style={styles.calloutText}>
            {shares
              ? "Items are assigned. Review the split and confirm to post it to the ledger."
              : "Assign each line item to the people who owe for it before confirming."}
          </Text>
          <Button
            title="Assign items"
            variant="secondary"
            onPress={() => router.push(`/expense/${expense.id}/assign`)}
            style={{ marginTop: spacing.sm }}
          />
          {shares && (
            <Button
              title="Confirm & split"
              onPress={onConfirm}
              loading={confirming}
              style={{ marginTop: spacing.sm }}
            />
          )}
          {confirmError ? (
            <Text style={styles.error}>{confirmError}</Text>
          ) : null}
        </View>
      )}

      <Text style={styles.sectionTitle}>Line items</Text>
      {expense.line_items.map((li) => (
        <View key={li.id} style={styles.lineRow}>
          <View style={{ flex: 1 }}>
            <Text style={styles.lineDesc}>
              {li.description || lineItemKindLabels[li.kind]}
            </Text>
            <Text style={styles.lineKind}>{lineItemKindLabels[li.kind]}</Text>
          </View>
          <Text style={styles.lineAmount}>
            {formatMoney(li.total_minor, expense.currency)}
          </Text>
        </View>
      ))}

      {shares && (
        <>
          <Text style={styles.sectionTitle}>Split</Text>
          {Object.entries(shares.shares).map(([userId, amount]) => (
            <View key={userId} style={styles.lineRow}>
              <Text style={styles.lineDesc}>{userId.slice(0, 8)}…</Text>
              <Text style={styles.lineAmount}>
                {formatMoney(amount, expense.currency)}
              </Text>
            </View>
          ))}
        </>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  headerRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  title: { fontSize: 22, fontWeight: "800", color: colors.text, flexShrink: 1 },
  total: {
    fontSize: 28,
    fontWeight: "800",
    color: colors.text,
    marginTop: spacing.xs,
  },
  sectionTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: colors.text,
    marginTop: spacing.lg,
    marginBottom: spacing.sm,
  },
  lineRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.sm + 4,
    marginBottom: spacing.xs,
  },
  lineDesc: { color: colors.text, fontSize: 14, fontWeight: "600" },
  lineKind: { color: colors.muted, fontSize: 12, marginTop: 2 },
  lineAmount: { color: colors.text, fontWeight: "700" },
  calloutInfo: {
    marginTop: spacing.md,
    padding: spacing.md,
    borderRadius: radius.md,
    backgroundColor: colors.primary + "11",
    borderWidth: 1,
    borderColor: colors.primary,
  },
  calloutWarning: {
    marginTop: spacing.md,
    padding: spacing.md,
    borderRadius: radius.md,
    backgroundColor: colors.warning + "11",
    borderWidth: 1,
    borderColor: colors.warning,
  },
  calloutDanger: {
    marginTop: spacing.md,
    padding: spacing.md,
    borderRadius: radius.md,
    backgroundColor: colors.danger + "11",
    borderWidth: 1,
    borderColor: colors.danger,
  },
  calloutText: { color: colors.text, fontSize: 13 },
  error: { color: colors.danger, marginTop: spacing.sm, fontSize: 13 },
  muted: { color: colors.muted, fontSize: 13 },
});
