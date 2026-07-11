import React from "react";
import { StyleSheet, Text, View } from "react-native";
import { router } from "expo-router";
import * as Clipboard from "expo-clipboard";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { useAuth } from "@/lib/auth";
import { colors, radius, spacing } from "@/lib/theme";

export default function ProfileScreen() {
  const { user, logout } = useAuth();
  if (!user) return null;

  return (
    <Screen>
      <Text style={styles.title}>Profile</Text>

      <View style={styles.card}>
        <Field label="Name" value={user.name} />
        <Field label="Email" value={user.email} />
        <Field label="User ID" value={user.id} mono />
        <Button
          title="Copy user ID"
          variant="secondary"
          onPress={() => Clipboard.setStringAsync(user.id)}
        />
      </View>

      <Text style={styles.hint}>
        Your user ID is handy for other people to add you to a group by ID until
        group invites by email ship.
      </Text>

      <Button
        title="Log out"
        variant="danger"
        onPress={async () => {
          await logout();
          router.replace("/(auth)/login");
        }}
        style={{ marginTop: spacing.lg }}
      />
    </Screen>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <View style={{ marginBottom: spacing.md }}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <Text style={[styles.fieldValue, mono && styles.mono]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  title: {
    fontSize: 24,
    fontWeight: "800",
    color: colors.text,
    marginBottom: spacing.md,
  },
  card: {
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
  },
  fieldLabel: { fontSize: 12, fontWeight: "600", color: colors.muted },
  fieldValue: { fontSize: 15, color: colors.text, marginTop: 2 },
  mono: { fontFamily: "monospace" },
  hint: { color: colors.muted, fontSize: 12, marginTop: spacing.md },
});
