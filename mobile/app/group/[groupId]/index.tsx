import React, { useCallback, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { useFocusEffect, router, useLocalSearchParams } from "expo-router";
import {
  formatMoney,
  type GroupResponse,
  type PairwiseBalance,
} from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Field } from "@/components/Field";
import { Button } from "@/components/Button";
import { apiClient } from "@/lib/api";
import { friendlyApiError } from "@/lib/auth";
import {
  getCachedGroupMembers,
  recordGroupMember,
  type CachedMember,
} from "@/lib/localIndex";
import { colors, radius, spacing } from "@/lib/theme";

export default function GroupDetailScreen() {
  const { groupId } = useLocalSearchParams<{ groupId: string }>();
  const [group, setGroup] = useState<GroupResponse | null>(null);
  const [balances, setBalances] = useState<PairwiseBalance[]>([]);
  const [members, setMembers] = useState<CachedMember[]>([]);
  const [newMemberId, setNewMemberId] = useState("");
  const [memberError, setMemberError] = useState<string | null>(null);
  const [addingMember, setAddingMember] = useState(false);

  const load = useCallback(async () => {
    if (!groupId) return;
    const [g, b, m] = await Promise.all([
      apiClient.getGroup(groupId),
      apiClient.getGroupBalances(groupId),
      getCachedGroupMembers(groupId),
    ]);
    setGroup(g);
    setBalances(b.balances);
    setMembers(m);
  }, [groupId]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  function memberLabel(userId: string): string {
    const m = members.find((x) => x.id === userId);
    return m ? m.name : `${userId.slice(0, 8)}…`;
  }

  async function onAddMember() {
    if (!groupId || !newMemberId.trim()) return;
    setMemberError(null);
    setAddingMember(true);
    try {
      const user = await apiClient.getUser(newMemberId.trim());
      await apiClient.addGroupMember(groupId, { user_id: user.id });
      await recordGroupMember(groupId, {
        id: user.id,
        name: user.name,
        email: user.email,
      });
      setNewMemberId("");
      await load();
    } catch (err) {
      setMemberError(friendlyApiError(err));
    } finally {
      setAddingMember(false);
    }
  }

  if (!group) {
    return (
      <Screen>
        <Text style={styles.muted}>Loading…</Text>
      </Screen>
    );
  }

  return (
    <Screen refreshing={false} onRefresh={load}>
      <Text style={styles.title}>{group.name}</Text>

      <View style={styles.actionsRow}>
        <Button
          title="Upload receipt"
          onPress={() =>
            router.push({
              pathname: "/expense/upload",
              params: { groupId: group.id },
            })
          }
        />
        <View style={{ width: spacing.sm }} />
        <Button
          title="Manual expense"
          variant="secondary"
          onPress={() =>
            router.push({
              pathname: "/expense/new",
              params: { groupId: group.id },
            })
          }
        />
      </View>

      <Text style={styles.sectionTitle}>Balances</Text>
      {balances.length === 0 ? (
        <Text style={styles.muted}>All settled up.</Text>
      ) : (
        balances.map((b, idx) => (
          <View key={idx} style={styles.row}>
            <Text style={styles.rowText}>
              {memberLabel(b.debtor_id)} owes {memberLabel(b.creditor_id)}
            </Text>
            <Text style={styles.rowAmount}>
              {formatMoney(b.net_amount_minor, "INR")}
            </Text>
          </View>
        ))
      )}

      <Text style={styles.sectionTitle}>Members</Text>
      {members.length === 0 ? (
        <Text style={styles.muted}>
          No members cached on this device yet (member list isn't fetchable from
          the backend — only newly-added members show here).
        </Text>
      ) : (
        members.map((m) => (
          <View key={m.id} style={styles.row}>
            <Text style={styles.rowText}>{m.name}</Text>
            <Text style={styles.muted}>{m.email}</Text>
          </View>
        ))
      )}

      <Field
        label="Add member by user ID"
        placeholder="Paste a Splitr user ID"
        autoCapitalize="none"
        value={newMemberId}
        onChangeText={setNewMemberId}
        error={memberError}
      />
      <Button
        title="Add member"
        variant="secondary"
        onPress={onAddMember}
        loading={addingMember}
      />
    </Screen>
  );
}

const styles = StyleSheet.create({
  title: {
    fontSize: 24,
    fontWeight: "800",
    color: colors.text,
    marginBottom: spacing.md,
  },
  actionsRow: { flexDirection: "row", marginBottom: spacing.lg },
  sectionTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: colors.text,
    marginTop: spacing.md,
    marginBottom: spacing.sm,
  },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  rowText: { color: colors.text, fontSize: 14 },
  rowAmount: { color: colors.text, fontWeight: "700" },
  muted: { color: colors.muted, fontSize: 13 },
});
