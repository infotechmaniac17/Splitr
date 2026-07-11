import React, { useState } from "react";
import { Text, View, StyleSheet } from "react-native";
import { Link, router } from "expo-router";
import { Screen } from "@/components/Screen";
import { Field } from "@/components/Field";
import { Button } from "@/components/Button";
import { useAuth, friendlyApiError } from "@/lib/auth";
import { colors, spacing } from "@/lib/theme";

export default function SignupScreen() {
  const { register } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit() {
    if (!name.trim() || !email.trim()) {
      setError("Name and email are required");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await register({
        name: name.trim(),
        email: email.trim(),
        password,
        phone: phone.trim() || undefined,
      });
      router.replace("/(tabs)/dashboard");
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Screen>
      <View style={styles.header}>
        <Text style={styles.title}>Create account</Text>
        <Text style={styles.subtitle}>Join or start splitting expenses</Text>
      </View>

      <Field
        label="Name"
        placeholder="Jane Doe"
        value={name}
        onChangeText={setName}
      />
      <Field
        label="Email"
        placeholder="jane@example.com"
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="email-address"
        value={email}
        onChangeText={setEmail}
      />
      <Field
        label="Password"
        placeholder="At least 8 characters"
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        value={password}
        onChangeText={setPassword}
      />
      <Field
        label="Phone (optional)"
        placeholder="+91 98765 43210"
        keyboardType="phone-pad"
        value={phone}
        onChangeText={setPhone}
        error={error}
      />

      <Button title="Sign up" onPress={onSubmit} loading={loading} />

      <View style={styles.footer}>
        <Text style={styles.footerText}>Already have an account? </Text>
        <Link href="/(auth)/login" style={styles.link}>
          Log in
        </Link>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { marginTop: spacing.xl, marginBottom: spacing.lg },
  title: { fontSize: 28, fontWeight: "800", color: colors.text },
  subtitle: { fontSize: 15, color: colors.muted, marginTop: spacing.xs },
  footer: {
    flexDirection: "row",
    justifyContent: "center",
    marginTop: spacing.lg,
  },
  footerText: { color: colors.muted },
  link: { color: colors.primary, fontWeight: "600" },
});
