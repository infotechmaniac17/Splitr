import React, { useState } from "react";
import { Text, StyleSheet } from "react-native";
import { router, useLocalSearchParams } from "expo-router";
import { toMinorUnits } from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Field } from "@/components/Field";
import { Button } from "@/components/Button";
import { useAuth, friendlyApiError } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { recordKnownExpense } from "@/lib/localIndex";
import { colors, spacing } from "@/lib/theme";

/**
 * Quick Manual Entry — ARCHITECTURE.md §3 edge-case table's "total-first"
 * fallback ("₹857 at Swiggy" is enough to save and split equally
 * immediately). Also reachable directly from the dashboard for expenses
 * with no PDF at all. Full line-item entry is out of scope for this first
 * pass (the item-assignment screen covers per-item splitting for
 * PDF-extracted expenses).
 */
export default function NewExpenseScreen() {
  const { user } = useAuth();
  const { groupId } = useLocalSearchParams<{ groupId?: string }>();
  const [vendor, setVendor] = useState("");
  const [amount, setAmount] = useState("");
  const [participantIds, setParticipantIds] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit() {
    if (!user) return;
    let totalMinor: number;
    try {
      totalMinor = toMinorUnits(amount, "INR");
      if (totalMinor <= 0) throw new Error("Amount must be greater than zero");
    } catch (err) {
      setError(friendlyApiError(err));
      return;
    }

    const extraParticipants = participantIds
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    setError(null);
    setLoading(true);
    try {
      const expense = await apiClient.createExpense({
        group_id: groupId || null,
        paid_by: user.id,
        vendor: vendor.trim() || null,
        currency: "INR",
        total_minor: totalMinor,
        participants: [user.id, ...extraParticipants],
      });
      await recordKnownExpense(user.id, expense.id);
      router.replace(`/expense/${expense.id}`);
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Screen>
      <Field label="Vendor (optional)" placeholder="Swiggy" value={vendor} onChangeText={setVendor} />
      <Field
        label="Total amount (INR)"
        placeholder="857.00"
        keyboardType="decimal-pad"
        value={amount}
        onChangeText={setAmount}
      />
      <Field
        label="Split with (comma-separated user IDs, optional)"
        placeholder="user-id-1, user-id-2"
        autoCapitalize="none"
        value={participantIds}
        onChangeText={setParticipantIds}
        error={error}
      />
      <Text style={styles.hint}>
        Splits equally among you and anyone you list here. Leave blank to
        record it as fully yours for now — you can add item-level detail
        later by uploading a receipt instead.
      </Text>
      <Button title="Save expense" onPress={onSubmit} loading={loading} />
    </Screen>
  );
}

const styles = StyleSheet.create({
  hint: { color: colors.muted, fontSize: 12, marginBottom: spacing.md },
});
