import React, { useCallback, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { useFocusEffect, router } from "expo-router";
import type { GroupResponse } from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { useAuth } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { getKnownGroupIds } from "@/lib/localIndex";
import { colors, radius, spacing } from "@/lib/theme";

export default function GroupsScreen() {
  const { user } = useAuth();
  const [groups, setGroups] = useState<GroupResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    try {
      const ids = await getKnownGroupIds(user.id);
      const results = await Promise.allSettled(ids.map((id) => apiClient.getGroup(id)));
      setGroups(
        results
          .filter((r): r is PromiseFulfilledResult<GroupResponse> => r.status === "fulfilled")
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

  return (
    <Screen refreshing={loading} onRefresh={load}>
      <View style={styles.header}>
        <Text style={styles.title}>Groups</Text>
        <Button title="New group" onPress={() => router.push("/group/new")} />
      </View>

      {groups.length === 0 ? (
        <View style={styles.emptyBox}>
          <Text style={styles.emptyText}>
            No groups yet. Groups let you split invoices with the same set of
            people repeatedly (e.g. "Goa Trip", "Flat 4B").
          </Text>
        </View>
      ) : (
        groups.map((g) => (
          <Pressable key={g.id} style={styles.row} onPress={() => router.push(`/group/${g.id}`)}>
            <Text style={styles.rowTitle}>{g.name}</Text>
            <Text style={styles.rowChevron}>{">"}</Text>
          </Pressable>
        ))
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: spacing.md,
  },
  title: { fontSize: 24, fontWeight: "800", color: colors.text },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  rowTitle: { fontSize: 15, fontWeight: "600", color: colors.text },
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
