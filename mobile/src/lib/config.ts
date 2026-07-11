import Constants from "expo-constants";

/**
 * API base URL for the FastAPI backend (see backend/app/main.py, mounted at
 * /api/v1 — @splitr/core's SplitrApiClient appends that prefix itself).
 *
 * Resolution order: EXPO_PUBLIC_API_BASE_URL env var (set per-environment,
 * e.g. `EXPO_PUBLIC_API_BASE_URL=https://api.splitr.app expo start`) ->
 * app.json `expo.extra.apiBaseUrl` -> localhost fallback for the Expo Go /
 * simulator dev loop.
 *
 * NOTE: "localhost" does not reach a host machine's backend from a physical
 * device or an Android emulator. Android emulators should use
 * http://10.0.2.2:8000; physical devices need the host machine's LAN IP.
 * Override via EXPO_PUBLIC_API_BASE_URL for those cases.
 */
export function getApiBaseUrl(): string {
  const fromEnv = process.env.EXPO_PUBLIC_API_BASE_URL;
  if (fromEnv) return fromEnv;

  const fromConfig = Constants.expoConfig?.extra?.apiBaseUrl;
  if (typeof fromConfig === "string" && fromConfig.length > 0)
    return fromConfig;

  return "http://localhost:8000";
}
