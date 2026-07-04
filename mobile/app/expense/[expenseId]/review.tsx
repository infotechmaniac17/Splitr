import React, { useCallback, useMemo, useState } from "react";
import { Linking, StyleSheet, Text, TextInput, View } from "react-native";
import { useFocusEffect, router, useLocalSearchParams } from "expo-router";
import {
  AllocationMethod,
  DiscountScope,
  LineItemKind,
  formatMoney,
  validationIssueLabels,
  type ExpenseResponse,
  type LineItemCreate,
  type RawExtraction,
  type ValidationIssue,
} from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { apiClient } from "@/lib/api";
import { friendlyApiError } from "@/lib/auth";
import { colors, radius, spacing } from "@/lib/theme";

/**
 * Mobile needs-review screen — parity with web/src/components/NeedsReviewView.tsx.
 * Reuses getRawExtraction / submitLineItemCorrection / pdfUrl from
 * @splitr/core's SplitrApiClient rather than forking correction logic.
 *
 * ASSUMPTION (Expo Go compatibility): the web app embeds the PDF in an
 * <iframe>. There is no in-app PDF/WebView renderer available without
 * adding a native dependency (react-native-webview is NOT bundled with
 * Expo Go by default), so this screen opens the PDF in the system browser
 * via Linking.openURL(apiClient.pdfUrl(expenseId)) instead. Revisit with an
 * embedded viewer once the project moves to a dev build.
 */

function lineItemsFromExpense(expense: ExpenseResponse): LineItemCreate[] {
  return expense.line_items.map((li) => ({
    line_no: li.line_no,
    kind: li.kind,
    description: li.description ?? "",
    quantity: li.quantity,
    unit_price_minor: li.unit_price_minor,
    total_minor: li.total_minor,
    allocation: li.allocation ?? undefined,
    discount_scope: li.discount_scope ?? undefined,
    parent_line_no: undefined,
  }));
}

const KIND_OPTIONS = Object.values(LineItemKind);

export default function ReviewScreen() {
  const { expenseId } = useLocalSearchParams<{ expenseId: string }>();
  const [expense, setExpense] = useState<ExpenseResponse | null>(null);
  const [rows, setRows] = useState<LineItemCreate[]>([]);
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [rawError, setRawError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!expenseId) return;
    const e = await apiClient.getExpense(expenseId);
    setExpense(e);
    setRows(lineItemsFromExpense(e));
    setRawError(null);
    try {
      const raw: RawExtraction = await apiClient.getRawExtraction(expenseId);
      const last = raw.attempts[raw.attempts.length - 1];
      setIssues(last?.validation?.issues ?? []);
    } catch {
      setIssues([]);
      setRawError("Could not load validation details for this expense.");
    }
  }, [expenseId]);

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load]),
  );

  const issuesByLine = useMemo(() => {
    const map = new Map<number | null, ValidationIssue[]>();
    for (const issue of issues) {
      const key = issue.line_no;
      map.set(key, [...(map.get(key) ?? []), issue]);
    }
    return map;
  }, [issues]);

  const invoiceLevelIssues = issuesByLine.get(null) ?? [];
  const linesSum = rows.reduce((sum, r) => sum + (r.total_minor || 0), 0);
  const reconciles = expense ? linesSum === expense.total_minor : false;

  function updateRow(idx: number, patch: Partial<LineItemCreate>) {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }

  function addRow() {
    setRows((prev) => [
      ...prev,
      {
        line_no: (prev.at(-1)?.line_no ?? 0) + 1,
        kind: LineItemKind.item,
        description: "",
        quantity: "1",
        unit_price_minor: null,
        total_minor: 0,
      },
    ]);
  }

  function removeRow(idx: number) {
    setRows((prev) => prev.filter((_, i) => i !== idx));
  }

  async function onOpenPdf() {
    if (!expenseId) return;
    const url = apiClient.pdfUrl(expenseId);
    try {
      await Linking.openURL(url);
    } catch {
      // no-op — nothing sensible to show the user if the OS can't open it
    }
  }

  async function onSubmit() {
    if (!expenseId) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const corrected = await apiClient.submitLineItemCorrection(expenseId, {
        line_items: rows,
      });
      router.replace(`/expense/${corrected.id}`);
    } catch (err) {
      setSubmitError(friendlyApiError(err));
    } finally {
      setSubmitting(false);
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
      <Text style={styles.title}>Needs review</Text>
      <Text style={styles.subtitle}>
        {expense.vendor || "This invoice"} didn't pass automatic validation. Fix the
        highlighted fields below, then resubmit.
      </Text>

      <Button title="View original PDF" variant="secondary" onPress={onOpenPdf} style={{ marginTop: spacing.sm }} />

      {invoiceLevelIssues.length > 0 && (
        <View style={styles.calloutDanger}>
          {invoiceLevelIssues.map((issue, i) => (
            <Text key={i} style={styles.calloutText}>
              • {issue.message || validationIssueLabels[issue.code]}
            </Text>
          ))}
        </View>
      )}
      {rawError && <Text style={styles.muted}>{rawError}</Text>}

      <Text style={styles.sectionTitle}>Line items</Text>
      {rows.map((row, idx) => {
        const lineIssues = issuesByLine.get(row.line_no ?? null) ?? [];
        const flagged = lineIssues.length > 0;
        return (
          <View key={idx} style={[styles.lineCard, flagged && styles.lineCardFlagged]}>
            {flagged && (
              <Text style={styles.flaggedText}>
                {lineIssues.map((i) => i.message).join("; ")}
              </Text>
            )}

            <Text style={styles.fieldLabel}>Kind</Text>
            <View style={styles.chipRow}>
              {KIND_OPTIONS.map((k) => (
                <Button
                  key={k}
                  title={k}
                  variant={row.kind === k ? "primary" : "secondary"}
                  onPress={() => updateRow(idx, { kind: k })}
                  style={styles.kindChip}
                />
              ))}
            </View>

            <Text style={styles.fieldLabel}>Description</Text>
            <TextInput
              value={row.description ?? ""}
              onChangeText={(v: string) => updateRow(idx, { description: v })}
              placeholder="Description"
              placeholderTextColor={colors.muted}
              style={styles.input}
            />

            <Text style={styles.fieldLabel}>Quantity</Text>
            <TextInput
              value={row.quantity}
              onChangeText={(v: string) => updateRow(idx, { quantity: v })}
              placeholder="Qty"
              placeholderTextColor={colors.muted}
              style={styles.input}
            />

            <Text style={styles.fieldLabel}>Unit price (minor units)</Text>
            <TextInput
              value={row.unit_price_minor != null ? String(row.unit_price_minor) : ""}
              onChangeText={(v: string) =>
                updateRow(idx, { unit_price_minor: v === "" ? null : Number(v) })
              }
              placeholder="Unit price (minor)"
              placeholderTextColor={colors.muted}
              keyboardType="numeric"
              style={styles.input}
            />

            <Text style={styles.fieldLabel}>Total (minor units)</Text>
            <TextInput
              value={String(row.total_minor)}
              onChangeText={(v: string) => updateRow(idx, { total_minor: Number(v) || 0 })}
              placeholder="Total (minor)"
              placeholderTextColor={colors.muted}
              keyboardType="numeric"
              style={styles.input}
            />

            {row.kind === LineItemKind.discount && (
              <>
                <Text style={styles.fieldLabel}>Discount scope</Text>
                <View style={styles.chipRow}>
                  {[DiscountScope.item, DiscountScope.cart].map((s) => (
                    <Button
                      key={s}
                      title={s}
                      variant={row.discount_scope === s ? "primary" : "secondary"}
                      onPress={() => updateRow(idx, { discount_scope: s })}
                      style={styles.kindChip}
                    />
                  ))}
                </View>
              </>
            )}

            {row.kind !== LineItemKind.item && row.kind !== LineItemKind.discount && (
              <>
                <Text style={styles.fieldLabel}>Allocation</Text>
                <View style={styles.chipRow}>
                  {[AllocationMethod.equal, AllocationMethod.proportional, AllocationMethod.manual].map(
                    (a) => (
                      <Button
                        key={a}
                        title={a}
                        variant={row.allocation === a ? "primary" : "secondary"}
                        onPress={() => updateRow(idx, { allocation: a })}
                        style={styles.kindChip}
                      />
                    ),
                  )}
                </View>
              </>
            )}

            <Button title="Remove row" variant="danger" onPress={() => removeRow(idx)} style={{ marginTop: spacing.sm }} />
          </View>
        );
      })}

      <Button title="+ Add line" variant="secondary" onPress={addRow} style={{ marginBottom: spacing.md }} />

      <View style={[styles.reconcileRow, reconciles ? styles.reconcileOk : styles.reconcileWarn]}>
        <Text style={styles.reconcileText}>Lines sum</Text>
        <Text style={styles.reconcileText}>
          {formatMoney(linesSum, expense.currency)} / {formatMoney(expense.total_minor, expense.currency)}
        </Text>
      </View>

      {submitError ? <Text style={styles.error}>{submitError}</Text> : null}

      <Button
        title="Resubmit for validation"
        onPress={onSubmit}
        loading={submitting}
        style={{ marginTop: spacing.md }}
      />
    </Screen>
  );
}

const styles = StyleSheet.create({
  title: { fontSize: 22, fontWeight: "800", color: colors.text },
  subtitle: { fontSize: 13, color: colors.muted, marginTop: spacing.xs },
  sectionTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: colors.text,
    marginTop: spacing.lg,
    marginBottom: spacing.sm,
  },
  fieldLabel: {
    fontSize: 12,
    fontWeight: "600",
    color: colors.muted,
    marginTop: spacing.sm,
    marginBottom: spacing.xs,
  },
  input: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.sm + 4,
    paddingVertical: spacing.sm,
    fontSize: 14,
    color: colors.text,
    backgroundColor: colors.card,
  },
  lineCard: {
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.sm + 4,
    marginBottom: spacing.sm,
  },
  lineCardFlagged: { borderColor: colors.danger, backgroundColor: colors.danger + "0d" },
  flaggedText: { color: colors.danger, fontSize: 12, fontWeight: "600", marginBottom: spacing.xs },
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
  kindChip: { paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, minHeight: 36, marginRight: spacing.xs, marginBottom: spacing.xs },
  reconcileRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    borderRadius: radius.md,
    paddingHorizontal: spacing.sm + 4,
    paddingVertical: spacing.sm + 2,
  },
  reconcileOk: { backgroundColor: colors.success + "22" },
  reconcileWarn: { backgroundColor: colors.warning + "22" },
  reconcileText: { fontSize: 13, fontWeight: "700", color: colors.text },
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
  muted: { color: colors.muted, fontSize: 13, marginTop: spacing.sm },
});
