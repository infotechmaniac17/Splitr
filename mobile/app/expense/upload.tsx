import React, { useEffect, useRef, useState } from "react";
import { StyleSheet, Text, View, Image, ActivityIndicator } from "react-native";
import { router, useLocalSearchParams } from "expo-router";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import type { ExpenseResponse } from "@splitr/core";
import { Screen } from "@/components/Screen";
import { Button } from "@/components/Button";
import { StatusBadge } from "@/components/StatusBadge";
import { useAuth, friendlyApiError } from "@/lib/auth";
import { apiClient } from "@/lib/api";
import { recordKnownExpense } from "@/lib/localIndex";
import { nextRouteForExpense } from "@/lib/expenseRouting";
import { colors, radius, spacing } from "@/lib/theme";

type PickedFile = { uri: string; name: string; mimeType: string };

const POLL_INTERVAL_MS = 2500;
const POLL_TIMEOUT_MS = 120_000;

export default function UploadScreen() {
  const { user } = useAuth();
  const { groupId } = useLocalSearchParams<{ groupId?: string }>();
  const [picked, setPicked] = useState<PickedFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [expense, setExpense] = useState<ExpenseResponse | null>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadline = useRef<number>(0);

  useEffect(() => {
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, []);

  async function pickPdf() {
    setError(null);
    const result = await DocumentPicker.getDocumentAsync({
      type: "application/pdf",
      copyToCacheDirectory: true,
    });
    if (result.canceled || !result.assets?.[0]) return;
    const asset = result.assets[0];
    setPicked({
      uri: asset.uri,
      name: asset.name,
      mimeType: asset.mimeType || "application/pdf",
    });
  }

  async function pickPhotoFromCamera() {
    setError(null);
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) {
      setError("Camera permission is required to photograph a receipt.");
      return;
    }
    const result = await ImagePicker.launchCameraAsync({
      mediaTypes: ["images"],
      quality: 0.8,
    });
    if (result.canceled || !result.assets?.[0]) return;
    const asset = result.assets[0];
    setPicked({
      uri: asset.uri,
      name: asset.fileName || "receipt.jpg",
      mimeType: asset.mimeType || "image/jpeg",
    });
  }

  async function pickPhotoFromLibrary() {
    setError(null);
    const perm = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!perm.granted) {
      setError(
        "Photo library permission is required to attach a receipt image.",
      );
      return;
    }
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ["images"],
      quality: 0.8,
    });
    if (result.canceled || !result.assets?.[0]) return;
    const asset = result.assets[0];
    setPicked({
      uri: asset.uri,
      name: asset.fileName || "receipt.jpg",
      mimeType: asset.mimeType || "image/jpeg",
    });
  }

  function startPolling(expenseId: string) {
    setPolling(true);
    // startPolling is only ever invoked from the onUpload button-press
    // handler (never during render), so this Date.now() read is safe;
    // the purity rule can't trace that through the helper-function call.
    // eslint-disable-next-line react-hooks/purity
    pollDeadline.current = Date.now() + POLL_TIMEOUT_MS;
    pollTimer.current = setInterval(async () => {
      try {
        const current = await apiClient.getExpense(expenseId);
        setExpense(current);
        if (current.parse_status !== "queued") {
          stopPolling();
          router.replace(nextRouteForExpense(current));
        } else if (Date.now() > pollDeadline.current) {
          stopPolling();
          setError("Still processing — check back from the dashboard shortly.");
        }
      } catch {
        // transient network error; keep polling until timeout
      }
    }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer.current) clearInterval(pollTimer.current);
    pollTimer.current = null;
    setPolling(false);
  }

  async function onUpload() {
    if (!user || !picked) return;
    setError(null);
    setUploading(true);
    try {
      // NOTE: this hits POST /expenses/upload, which is an M4 ASSUMPTION —
      // not part of the frozen v1 API_CONTRACT.md (upload was explicitly
      // out of scope there) and not yet implemented on the backend. See
      // @splitr/core/src/client.ts's uploadExpensePdf docstring. Expect a
      // 404 until the backend adds this route; wired now so mobile is ready
      // the moment it lands.
      //
      // React Native's fetch/FormData accepts {uri, name, type} objects for
      // file parts (there is no browser File/Blob for local device URIs).
      // SplitrApiClient's signature expects a Blob for web parity; the
      // shape below is runtime-compatible even though it isn't a real Blob
      // instance, so we cast at the call boundary.
      const filePart = {
        uri: picked.uri,
        name: picked.name,
        type: picked.mimeType,
      } as unknown as Blob;

      const created = await apiClient.uploadExpensePdf({
        file: filePart,
        filename: picked.name,
        paidBy: user.id,
        groupId: groupId || null,
      });
      setExpense(created);
      await recordKnownExpense(user.id, created.id);

      if (created.parse_status === "queued") {
        startPolling(created.id);
      } else {
        router.replace(nextRouteForExpense(created));
      }
    } catch (err) {
      setError(friendlyApiError(err));
    } finally {
      setUploading(false);
    }
  }

  const isImage = picked?.mimeType.startsWith("image/");

  return (
    <Screen>
      <Text style={styles.title}>Upload a receipt</Text>
      <Text style={styles.subtitle}>
        PDF invoices (Amazon, Flipkart) or a photo of a printed/screenshot
        receipt (Swiggy, Zomato, Zepto, Blinkit) both work — photos go through
        the vision extraction path.
      </Text>

      <View style={styles.pickerRow}>
        <Button title="Take photo" onPress={pickPhotoFromCamera} />
      </View>
      <View style={styles.pickerRow}>
        <Button
          title="Choose photo"
          variant="secondary"
          onPress={pickPhotoFromLibrary}
        />
      </View>
      <View style={styles.pickerRow}>
        <Button title="Choose PDF" variant="secondary" onPress={pickPdf} />
      </View>

      {picked ? (
        <View style={styles.previewCard}>
          {isImage ? (
            <Image
              source={{ uri: picked.uri }}
              style={styles.previewImage}
              resizeMode="cover"
            />
          ) : (
            <Text style={styles.previewFileName}>{picked.name}</Text>
          )}
          <Button
            title={uploading ? "Uploading…" : "Upload"}
            onPress={onUpload}
            loading={uploading}
            style={{ marginTop: spacing.sm }}
          />
        </View>
      ) : null}

      {error ? <Text style={styles.error}>{error}</Text> : null}

      {polling ? (
        <View style={styles.pollingBox}>
          <ActivityIndicator color={colors.primary} />
          <Text style={styles.pollingText}>Extracting line items…</Text>
          {expense ? <StatusBadge status={expense.parse_status} /> : null}
        </View>
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  title: {
    fontSize: 22,
    fontWeight: "800",
    color: colors.text,
    marginBottom: spacing.xs,
  },
  subtitle: { fontSize: 13, color: colors.muted, marginBottom: spacing.lg },
  pickerRow: { marginBottom: spacing.sm },
  previewCard: {
    marginTop: spacing.md,
    padding: spacing.md,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
  },
  previewImage: { width: "100%", height: 200, borderRadius: radius.sm },
  previewFileName: { fontSize: 14, color: colors.text },
  error: { color: colors.danger, marginTop: spacing.sm },
  pollingBox: {
    marginTop: spacing.lg,
    alignItems: "center",
    gap: spacing.sm,
  },
  pollingText: { color: colors.muted },
});
