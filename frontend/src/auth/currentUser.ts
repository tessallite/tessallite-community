/**
 * Read the current user's role from localStorage and answer common
 * "can this user see X?" questions.
 *
 * SECURITY MODEL: localStorage role is set exclusively from /users/me
 * server responses (Bug-837 fix). The backend always re-checks every API
 * call via JWT; these helpers exist only so the SPA hides UI surfaces the
 * user can't act on. They return false conservatively when the role is
 * missing (logged-out / corrupted state). XSS-based localStorage
 * manipulation can show admin UI but cannot bypass backend authorisation.
 */

export type UserRole =
  | "system_admin"
  | "tenant_admin"
  | "modeler"
  | "viewer"
  | "analyst"
  | "member"
  | "model_technical";

const STORAGE_KEY = "user_role";

export function currentUserRole(): UserRole | null {
  if (typeof window === "undefined") return null;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (!v) return null;
    return v as UserRole;
  } catch {
    console.warn('Could not read localStorage key "user_role", using default.');
    return null;
  }
}

export function isSystemAdmin(): boolean {
  return currentUserRole() === "system_admin";
}

export function isTenantAdmin(): boolean {
  const r = currentUserRole();
  return r === "system_admin" || r === "tenant_admin";
}

export function canEditModelConfig(): boolean {
  // Only project-editor tiers see the edit affordance: tenant_admin and above
  // always; modelers via their per-model binding (the backend resolves it).
  // member / analyst / model_technical are NOT editor tiers — the backend
  // require_role("modeler") gate rejects them, so showing the tab only misled
  // the user. Hiding it matches the centralized role taxonomy (F-021-12) and
  // keeps the UI fail-safe. The backend remains authoritative either way.
  const r = currentUserRole();
  return (
    r === "system_admin"
    || r === "tenant_admin"
    || r === "modeler"
  );
}
