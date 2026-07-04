import React, { useCallback, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, router, useLocalSearchParams } from "expo-router";
import { formatMoney, type ExpenseResponse, type LineItemResponse } from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Field } from "@/components/Field";
import { Button } from "@/components/Button";
import { apiClient } from "@/lib/api";
import { useAuth, friendlyApiError } from "@/lib/auth";
import { getCachedGroupMembers, recordGroupMember, type CachedMember } from "@/lib/localIndex";
import { colors, radius, spacing } from "@/lib/theme";

/**
 * Item-assignment screen. Only "item" (and item-scoped discount/refund)
 * line kinds are assignable — cart-level rows (tax, delivery_fee,
 * platform_fee, packing_fee, tip, cart discount) are allocated
 * automatically by the splitting engine's proportional/equal/manual
 * `allocation` rule once item assignments exist (ARCHITECTURE.md §4), so
 * they're shown read-only here.
 *
 * Assigning to a subgroup is documented as "UI sugar" that expands to one
 * row per member (API_CONTRACT.md §... / PUT /expenses/{id}/assignments
 * docstring) — subgroups have no CRUD endpoints in the current API
 * contract, so this screen assigns to individual members only; flagged as
 * a gap for whenever subgroup endpoints ship.
 */
export default function AssignScreen() {
  const { user } = useAuth();
  const { expenseId } = useLocalSearchParams<{ expenseId: string }>();
  const [expense, setExpense] = useState<ExpenseResponse | null>(null);
  const [participants, setParticipants] = useState<CachedMember[]>([]);
  const [newParticipantId, setNewParticipantId] = useState("");
  const [participantError, setParticipantError] = useState<string | null>(null);
  const [selections, setSelections] = useState<Record<string, Set<string>>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!expenseId || !user) return;
    const e = await apiClient.getExpense(expenseId);
    setExpense(e);

    const cached = e.group_id ? await getCachedGroupMembers(e.group_id) : [];
    const base: CachedMember[] = cached.some((m) => m.id === user.id)
      ? cached
      : [{ id: user.id, name: user.name, email: user.email }, ...cached];
    setParticipants(base);
  }, [expenseId, user]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  const assignableLines = useMemo(
    () =>
      (expense?.line_items ?? []).filter(
        (li) => li.kind === "item" || li.discount_scope === "item" || li.kind === "refund",
      ),
    [expense],
  );
  const cartLevelLines = useMemo(
    () => (expense?.line_items ?? []).filter((li) => !assignableLines.includes(li)),
    [expense, assignableLines],
  );

  function toggle(lineItemId: string, userId: string) {
    setSelections((prev) => {
      const next = { ...prev };
      const set = new Set(next[lineItemId] ?? []);
      if (set.has(userId)) set.delete(userId);
      else set.add(userId);
      next[lineItemId] = set;
      return next;
    });
  }

  async function onAddParticipant() {
    if (!newParticipantId.trim() || !expense) return;
    setParticipantError(null);
    try {
      const found = await apiClient.getUser(newParticipantId.trim());
      setParticipants((prev) =>
        prev.some((p) => p.id === found.id) ? prev : [...prev, { id: found.id, name: found.name, email: found.email }],
      );
      if (expense.group_id) {
        await recordGroupMember(expense.group_id, {
          id: found.id,
          name: found.name,
          email: found.email,
        });
      }
      setNewParticipantId("");
    } catch (err) {
      setParticipantError(friendlyApiError(err));
    }
  }

  async function onSave() {
    if (!expenseId) return;
    const assignments = Object.entries(selections).flatMap(([lineItemId, userIds]) =>
      Array.from(userIds).map((userId) => ({
        line_item_id: lineItemId,
        user_id: userId,
        weight: "1",
      })),
    );
    if (assignments.length === 0) {
      setSaveError("Assign at least one item to at least one person.");
      return;
    }
    setSaveError(null);
    setSaving(true);
    try {
      await apiClient.putAssignments(expenseId, { assignments });
      router.replace(`/expense/${expenseId}`);
    } catch (err) {
      setSaveError(friendlyApiError(err));
    } finally {
      setSaving(false);
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
      <Text style={styles.title}>Assign items</Text>
      <Text style={styles.subtitle}>Tap a person's chip on each line to include them.</Text>

      <Field
        label="Add person by user ID"
        placeholder="Paste a Splitr user ID"
        autoCapitalize="none"
        value={newParticipantId}
        onChangeText={setNewParticipantId}
        error={participantError}
      />
      <Button title="Add" variant="secondary" onPress={onAddParticipant} />

      {assignableLines.map((li) => (
        <LineAssignRow
          key={li.id}
          line={li}
          currency={expense.currency}
          participants={participants}
          selected={selections[li.id] ?? new Set()}
          onToggle={(userId) => toggle(li.id, userId)}
        />
      ))}

      {cartLevelLines.length > 0 && (
        <>
          <Text style={styles.sectionTitle}>Automatically split</Text>
          {cartLevelLines.map((li) => (
            <View key={li.id} style={styles.cartRow}>
              <Text style={styles.cartDesc}>{li.description || li.kind}</Text>
              <Text style={styles.cartAmount}>{formatMoney(li.total_minor, expense.currency)}</Text>
            </View>
          ))}
        </>
      )}

      {saveError ? <Text style={styles.error}>{saveError}</Text> : null}
      <Button title="Save assignments" onPress={onSave} loading={saving} style={{ marginTop: spacing.md }} />
    </Screen>
  );
}

function LineAssignRow({
  line,
  currency,
  participants,
  selected,
  onToggle,
}: {
  line: LineItemResponse;
  currency: string;
  participants: CachedMember[];
  selected: Set<string>;
  onToggle: (userId: string) => void;
}) {
  return (
    <View style={styles.lineCard}>
      <View style={styles.lineHeader}>
        <Text style={styles.lineDesc}>{line.description || line.kind}</Text>
        <Text style={styles.lineAmount}>{formatMoney(line.total_minor, currency)}</Text>
      </View>
      <View style={styles.chipRow}>
        {participants.map((p) => {
          const active = selected.has(p.id);
          return (
            <Pressable
              key={p.id}
              onPress={() => onToggle(p.id)}
              style={[styles.chip, active && styles.chipActive]}
            >
              <Text style={[styles.chipText, active && styles.chipTextActive]}>{p.name}</Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  title: { fontSize: 22, fontWeight: "800", color: colors.text },
  subtitle: { fontSize: 13, color: colors.muted, marginBottom: spacing.md },
  sectionTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: colors.text,
    marginTop: spacing.lg,
    marginBottom: spacing.sm,
  },
  lineCard: {
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.sm + 4,
    marginBottom: spacing.sm,
  },
  lineHeader: { flexDirection: "row", justifyContent: "space-between", marginBottom: spacing.sm },
  lineDesc: { color: colors.text, fontSize: 14, fontWeight: "600", flexShrink: 1 },
  lineAmount: { color: colors.text, fontWeight: "700" },
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
  chip: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.lg,
    paddingHorizontal: spacing.sm + 2,
    paddingVertical: spacing.xs,
    marginRight: spacing.xs,
    marginBottom: spacing.xs,
  },
  chipActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  chipText: { fontSize: 13, color: colors.text },
  chipTextActive: { color: "#fff", fontWeight: "600" },
  cartRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: spacing.xs,
  },
  cartDesc: { color: colors.muted, fontSize: 13 },
  cartAmount: { color: colors.muted, fontSize: 13 },
  error: { color: colors.danger, marginTop: spacing.sm },
  muted: { color: colors.muted, fontSize: 13 },
});
