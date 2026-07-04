import React, { useCallback, useState } from "react";
import { StyleSheet, Text, View, Pressable } from "react-native";
import { useFocusEffect, router } from "expo-router";
import { formatMoney, type ExpenseResponse, type GroupResponse } from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { StatusBadge } from "@/components/StatusBadge";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { getKnownExpenseIds, getKnownGroupIds } from "@/lib/localIndex";
import { colors, radius, spacing } from "@/lib/theme";
import { nextRouteForExpense } from "@/lib/expenseRouting";

export default function DashboardScreen() {
  const { user } = useAuth();
  const [netBalance, setNetBalance] = useState<number | null>(null);
  const [groups, setGroups] = useState<GroupResponse[]>([]);
  const [expenses, setExpenses] = useState<ExpenseResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    try {
      const [balance, groupIds, expenseIds] = await Promise.all([
        apiClient.getUserBalance(user.id),
        getKnownGroupIds(user.id),
        getKnownExpenseIds(user.id),
      ]);
      setNetBalance(balance.net_balance_minor);

      const groupResults = await Promise.allSettled(groupIds.map((id) => apiClient.getGroup(id)));
      setGroups(
        groupResults
          .filter((r): r is PromiseFulfilledResult<GroupResponse> => r.status === "fulfilled")
          .map((r) => r.value),
      );

      const expenseResults = await Promise.allSettled(
        expenseIds.slice(0, 20).map((id) => apiClient.getExpense(id)),
      );
      setExpenses(
        expenseResults
          .filter((r): r is PromiseFulfilledResult<ExpenseResponse> => r.status === "fulfilled")
          .map((r) => r.value),
      );
    } finally {
      setLoading(false);
    }
  }, [user]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  if (!user) return null;

  return (
    <Screen refreshing={loading} onRefresh={load}>
      <Text style={styles.greeting}>Hi, {user.name.split(" ")[0]}</Text>

      <View style={styles.balanceCard}>
        <Text style={styles.balanceLabel}>
          {netBalance !== null && netBalance < 0 ? "You owe" : "You're owed"}
        </Text>
        <Text
          style={[
            styles.balanceAmount,
            { color: (netBalance ?? 0) < 0 ? colors.danger : colors.success },
          ]}
        >
          {formatMoney(Math.abs(netBalance ?? 0), "INR")}
        </Text>
      </View>

      <View style={styles.actionsRow}>
        <Button title="Upload receipt" onPress={() => router.push("/expense/upload")} />
        <View style={{ width: spacing.sm }} />
        <Button
          title="Manual expense"
          variant="secondary"
          onPress={() => router.push("/expense/new")}
        />
      </View>

      <SectionHeader title="Your groups" onAdd={() => router.push("/group/new")} />
      {groups.length === 0 ? (
        <EmptyHint text="No groups yet. Create one to start splitting with friends." />
      ) : (
        groups.map((g) => (
          <Pressable
            key={g.id}
            style={styles.row}
            onPress={() => router.push(`/group/${g.id}`)}
          >
            <Text style={styles.rowTitle}>{g.name}</Text>
            <Text style={styles.rowChevron}>{">"}</Text>
          </Pressable>
        ))
      )}

      <SectionHeader title="Recent expenses" />
      {expenses.length === 0 ? (
        <EmptyHint text="No expenses yet. Upload a receipt or add one manually." />
      ) : (
        expenses.map((e) => (
          <Pressable
            key={e.id}
            style={styles.row}
            onPress={() => router.push(nextRouteForExpense(e))}
          >
            <View style={{ flex: 1 }}>
              <Text style={styles.rowTitle}>{e.vendor || "Expense"}</Text>
              <Text style={styles.rowSubtitle}>{formatMoney(e.total_minor, e.currency)}</Text>
            </View>
            <StatusBadge status={e.parse_status} />
          </Pressable>
        ))
      )}
    </Screen>
  );
}

function SectionHeader({ title, onAdd }: { title: string; onAdd?: () => void }) {
  return (
    <View style={styles.sectionHeader}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {onAdd ? (
        <Pressable onPress={onAdd}>
          <Text style={styles.addLink}>+ Add</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

function EmptyHint({ text }: { text: string }) {
  return (
    <View style={styles.emptyBox}>
      <Text style={styles.emptyText}>{text}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  greeting: { fontSize: 24, fontWeight: "800", color: colors.text, marginBottom: spacing.md },
  balanceCard: {
    backgroundColor: colors.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    marginBottom: spacing.md,
  },
  balanceLabel: { color: colors.muted, fontSize: 13, fontWeight: "600" },
  balanceAmount: { fontSize: 32, fontWeight: "800", marginTop: spacing.xs },
  actionsRow: { flexDirection: "row", marginBottom: spacing.lg },
  sectionHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginTop: spacing.md,
    marginBottom: spacing.sm,
  },
  sectionTitle: { fontSize: 16, fontWeight: "700", color: colors.text },
  addLink: { color: colors.primary, fontWeight: "600" },
  row: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  rowTitle: { fontSize: 15, fontWeight: "600", color: colors.text },
  rowSubtitle: { fontSize: 13, color: colors.muted, marginTop: 2 },
  rowChevron: { color: colors.muted },
  emptyBox: {
    padding: spacing.md,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    borderStyle: "dashed",
  },
  emptyText: { color: colors.muted, fontSize: 13, textAlign: "center" },
});
