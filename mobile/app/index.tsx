import React from "react";
import { ActivityIndicator, View } from "react-native";
import { Redirect } from "expo-router";
import { useAuth } from "@/lib/auth";
import { colors } from "@/lib/theme";

/**
 * Entry route. Gates on device-local auth state (see src/lib/auth.tsx) and
 * redirects to the auth stack or the main tabs — this is the
 * dashboard -> upload -> assignment -> needs-review priority ordering's
 * root; the dashboard tab is the landing screen once authenticated.
 */
export default function Index() {
  const { user, isLoading } = useAuth();

  if (isLoading) {
    return (
      <View
        style={{
          flex: 1,
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: colors.bg,
        }}
      >
        <ActivityIndicator size="large" color={colors.primary} />
      </View>
    );
  }

  return <Redirect href={user ? "/(tabs)/dashboard" : "/(auth)/login"} />;
}
