"use client";

/**
 * M4 ASSUMPTION / GAP: the backend (M1-M3) exposes create/get/balances for
 * groups and create/get for expenses, but no "list my groups" or "list
 * expenses in this group" endpoints (see backend/app/api/groups.py,
 * expenses.py — there is no GET /groups or GET /groups/{id}/expenses).
 * ARCHITECTURE.md's dashboard requirement ("list groups, list expenses")
 * therefore can't be fully served from the server today.
 *
 * Stopgap: remember groups/expenses this browser has created or visited in
 * localStorage, scoped per acting user, so the dashboard has something to
 * render. Each entry is re-fetched from the API (GET /groups/{id} etc.) so
 * displayed data is always live — only the *set of ids* is client-cached.
 * Swap this out once the backend adds proper list endpoints.
 */

interface RememberedGroup {
  id: string;
  name: string;
}
interface RememberedExpense {
  id: string;
  groupId: string | null;
}

function key(userId: string, kind: "groups" | "expenses"): string {
  return `splitr:${kind}:${userId}`;
}

function read<T>(storageKey: string): T[] {
  try {
    const raw = window.localStorage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as T[]) : [];
  } catch {
    return [];
  }
}

function write<T>(storageKey: string, items: T[]): void {
  window.localStorage.setItem(storageKey, JSON.stringify(items));
}

export function rememberGroup(userId: string, group: RememberedGroup): void {
  const storageKey = key(userId, "groups");
  const existing = read<RememberedGroup>(storageKey);
  if (existing.some((g) => g.id === group.id)) return;
  write(storageKey, [...existing, group]);
}

export function listRememberedGroups(userId: string): RememberedGroup[] {
  return read<RememberedGroup>(key(userId, "groups"));
}

export function rememberExpense(
  userId: string,
  expense: RememberedExpense,
): void {
  const storageKey = key(userId, "expenses");
  const existing = read<RememberedExpense>(storageKey);
  if (existing.some((e) => e.id === expense.id)) return;
  write(storageKey, [expense, ...existing].slice(0, 50));
}

export function listRememberedExpenses(userId: string): RememberedExpense[] {
  return read<RememberedExpense>(key(userId, "expenses"));
}

// ---------------------------------------------------------------------------
// Group member display shape, shared by AssignmentScreen and the pages that
// fetch the roster via api.getGroupMembers() (GET /groups/{id}/members).
// ---------------------------------------------------------------------------

export interface RememberedMember {
  id: string;
  name: string;
}
