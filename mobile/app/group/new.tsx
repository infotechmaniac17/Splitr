import React, { useState } from "react";
import { View } from "react-native";
import { router } from "expo-router";
import { Screen } from "@/components/Screen";
import { Field } from "@/components/Field";
import { Button } from "@/components/Button";
import { useAuth, friendlyApiError } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { recordKnownGroup } from "@/lib/localIndex";

export default function NewGroupScreen() {
  const { user } = useAuth();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit() {
    if (!user) return;
    if (!name.trim()) {
      setError("Group name is required");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const group = await apiClient.createGroup({
        name: name.trim(),
        created_by: user.id,
        simplify_debts: true,
      });
      await recordKnownGroup(user.id, group.id);
      router.replace(`/group/${group.id}`);
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Screen>
      <View>
        <Field
          label="Group name"
          placeholder="Goa Trip"
          value={name}
          onChangeText={setName}
          error={error}
        />
        <Button title="Create group" onPress={onSubmit} loading={loading} />
      </View>
    </Screen>
  );
}
