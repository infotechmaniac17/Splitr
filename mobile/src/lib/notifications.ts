import { Platform } from "react-native";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import Constants from "expo-constants";
import { getApiBaseUrl } from "./config";
import { getAccessToken } from "./session";

/**
 * Push notifications wiring (expo-notifications) for the two push events
 * called out in the task brief: "parse-complete" and "you owe / you're
 * owed" balance-change events.
 *
 * GAP: the backend does not yet expose a push-token registration endpoint
 * or emit these push events — there is no `/users/{id}/push-tokens` route
 * in backend/app/api/users.py, and Celery's extraction pipeline
 * (app/extraction/tasks.py) has no push/notification side effect today.
 * This module still performs the client-side half (permission request +
 * Expo push token retrieval) so the wiring is ready to flip on the moment
 * the backend adds the endpoint — the POST below is written against the
 * most natural REST shape for it and will 404 harmlessly until then.
 */

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: false,
    shouldSetBadge: false,
  }),
});

export async function registerForPushNotificationsAsync(
  userId: string,
): Promise<string | null> {
  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("default", {
      name: "default",
      importance: Notifications.AndroidImportance.DEFAULT,
    });
  }

  if (!Device.isDevice) {
    // Push tokens aren't available on simulators/web.
    return null;
  }

  const { status: existingStatus } = await Notifications.getPermissionsAsync();
  let finalStatus = existingStatus;
  if (existingStatus !== "granted") {
    const { status } = await Notifications.requestPermissionsAsync();
    finalStatus = status;
  }
  if (finalStatus !== "granted") {
    return null;
  }

  const projectId =
    Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  const tokenResponse = await Notifications.getExpoPushTokenAsync(
    projectId ? { projectId } : undefined,
  );
  const token = tokenResponse.data;

  // ASSUMPTION: POST /api/v1/users/{id}/push-tokens — not implemented on the
  // backend yet (see module docstring). Swallow errors so a 404 here never
  // breaks login/app usage.
  try {
    const accessToken = getAccessToken();
    await fetch(`${getApiBaseUrl()}/api/v1/users/${userId}/push-tokens`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify({ token, platform: Platform.OS }),
    });
  } catch {
    // Backend route not implemented yet / offline — non-fatal.
  }

  return token;
}
