const SESSION_EXPIRY_KEY = "session_expires_at";

export function setSessionExpiry(maxAgeSeconds: number): void {
  localStorage.setItem(
    SESSION_EXPIRY_KEY,
    String(Date.now() + maxAgeSeconds * 1000),
  );
}

export function getSessionExpiry(): number | null {
  try {
    const v = localStorage.getItem(SESSION_EXPIRY_KEY);
    return v ? Number(v) : null;
  } catch {
    return null;
  }
}

export function isSessionExpired(): boolean {
  const exp = getSessionExpiry();
  if (exp === null) return false;
  return exp < Date.now();
}

export function isSessionExpiringSoon(graceSeconds = 60): boolean {
  const exp = getSessionExpiry();
  if (exp === null) return false;
  const remaining = exp - Date.now();
  return remaining > 0 && remaining < graceSeconds * 1000;
}

export function clearSession(): void {
  localStorage.removeItem("tenant_id");
  localStorage.removeItem("user_role");
  localStorage.removeItem(SESSION_EXPIRY_KEY);
}

export function dispatchSessionExpired(): void {
  window.dispatchEvent(new CustomEvent("tessallite:session-expired"));
}
