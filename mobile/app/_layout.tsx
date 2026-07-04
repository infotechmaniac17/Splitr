import React, { useEffect } from "react";
import { Stack, router, useSegments } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { AuthProvider, useAuth } from "@/lib/auth";

/**
 * Global auth gate: redirects to the login stack the moment `user` becomes
 * null (explicit logout, or a silent-refresh/401-retry failure from
 * session.ts's onSessionExpired hook — see lib/auth.tsx) from *any* screen,
 * not just the initial `app/index.tsx` redirect. Without this, a token
 * expiring while the user is several screens deep (e.g. expense/[id]/assign)
 * would just leave them looking at a screen full of failed requests.
 */
function AuthGate() {
  const { user, isLoading } = useAuth();
  const segments = useSegments();

  useEffect(() => {
    if (isLoading) return;
    const inAuthGroup = segments[0] === "(auth)";
    if (!user && !inAuthGroup) {
      router.replace("/(auth)/login");
    }
  }, [user, isLoading, segments]);

  return null;
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <AuthProvider>
        <AuthGate />
        <StatusBar style="dark" />
        <Stack screenOptions={{ headerShown: false }}>
          <Stack.Screen name="(auth)" />
          <Stack.Screen name="(tabs)" />
          <Stack.Screen
            name="group/[groupId]/index"
            options={{ headerShown: true, title: "Group" }}
          />
          <Stack.Screen
            name="group/new"
            options={{ headerShown: true, title: "New group", presentation: "modal" }}
          />
          <Stack.Screen
            name="expense/new"
            options={{ headerShown: true, title: "New expense", presentation: "modal" }}
          />
          <Stack.Screen
            name="expense/upload"
            options={{ headerShown: true, title: "Upload receipt", presentation: "modal" }}
          />
          <Stack.Screen
            name="expense/[expenseId]/index"
            options={{ headerShown: true, title: "Expense" }}
          />
          <Stack.Screen
            name="expense/[expenseId]/assign"
            options={{ headerShown: true, title: "Assign items" }}
          />
          <Stack.Screen
            name="expense/[expenseId]/review"
            options={{ headerShown: true, title: "Needs review" }}
          />
        </Stack>
      </AuthProvider>
    </SafeAreaProvider>
  );
}
