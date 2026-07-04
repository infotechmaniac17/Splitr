import React from "react";
import { StyleSheet, Text, View } from "react-native";
import { parseStatusLabels, type ParseStatus } from "@splitr/core";
import { colors, radius, spacing } from "@/lib/theme";

const STATUS_COLORS: Record<ParseStatus, string> = {
  queued: colors.muted,
  parsed: colors.primary,
  needs_review: colors.warning,
  confirmed: colors.success,
  failed: colors.danger,
};

export function StatusBadge({ status }: { status: ParseStatus }) {
  const tint = STATUS_COLORS[status];
  return (
    <View style={[styles.badge, { backgroundColor: tint + "22", borderColor: tint }]}>
      <Text style={[styles.text, { color: tint }]}>{parseStatusLabels[status]}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    borderWidth: 1,
    borderRadius: radius.sm,
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
    alignSelf: "flex-start",
  },
  text: { fontSize: 12, fontWeight: "600" },
});
