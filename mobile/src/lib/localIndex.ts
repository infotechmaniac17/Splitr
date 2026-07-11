import AsyncStorage from "@react-native-async-storage/async-storage";

/**
 * GAP: the backend has no list endpoints — no `GET /groups` (groups for a
 * user), no `GET /groups/{id}/expenses`, no `GET /groups/{id}/members`
 * (only `POST /groups/{id}/members` to add one; see backend/app/api/*.py
 * and API_CONTRACT.md, which only documents item/expense/group-by-id
 * shapes). The dashboard and assignment screens still need "which groups
 * am I in", "which expenses exist", and "who is in this group" to be
 * usable.
 *
 * This module is a device-local index (AsyncStorage, non-sensitive) that
 * records IDs the app already learned about via real API responses
 * (group/expense creation, add-member calls) so those objects can be
 * re-fetched with the real per-ID GET endpoints. It is NOT a substitute for
 * server-side list endpoints — it only reflects what this device has seen,
 * won't show a group/expense another member created, and resets if the app
 * is reinstalled. Flagged in the M5 report as a backend gap: adding
 * `GET /users/{id}/groups`, `GET /groups/{id}/expenses`, and
 * `GET /groups/{id}/members` would let this be replaced with real queries.
 */

interface CachedMember {
  id: string;
  name: string;
  email: string;
}

const keys = {
  groups: (userId: string) => `splitr.index.groups.${userId}`,
  expenses: (userId: string) => `splitr.index.expenses.${userId}`,
  members: (groupId: string) => `splitr.index.members.${groupId}`,
};

async function readList(key: string): Promise<string[]> {
  const raw = await AsyncStorage.getItem(key);
  if (!raw) return [];
  try {
    return JSON.parse(raw) as string[];
  } catch {
    return [];
  }
}

async function appendUnique(key: string, value: string): Promise<void> {
  const list = await readList(key);
  if (!list.includes(value)) {
    list.unshift(value);
    await AsyncStorage.setItem(key, JSON.stringify(list));
  }
}

export async function recordKnownGroup(
  userId: string,
  groupId: string,
): Promise<void> {
  await appendUnique(keys.groups(userId), groupId);
}

export async function getKnownGroupIds(userId: string): Promise<string[]> {
  return readList(keys.groups(userId));
}

export async function recordKnownExpense(
  userId: string,
  expenseId: string,
): Promise<void> {
  await appendUnique(keys.expenses(userId), expenseId);
}

export async function getKnownExpenseIds(userId: string): Promise<string[]> {
  return readList(keys.expenses(userId));
}

export async function recordGroupMember(
  groupId: string,
  member: CachedMember,
): Promise<void> {
  const raw = await AsyncStorage.getItem(keys.members(groupId));
  const list: CachedMember[] = raw ? JSON.parse(raw) : [];
  if (!list.some((m) => m.id === member.id)) {
    list.push(member);
    await AsyncStorage.setItem(keys.members(groupId), JSON.stringify(list));
  }
}

export async function getCachedGroupMembers(
  groupId: string,
): Promise<CachedMember[]> {
  const raw = await AsyncStorage.getItem(keys.members(groupId));
  return raw ? (JSON.parse(raw) as CachedMember[]) : [];
}

export type { CachedMember };
