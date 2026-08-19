import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n";
import { safeLocalGet } from "../utils/safeLocalStorage";
import { Navigate } from "react-router-dom";
import { Box, CircularProgress, Snackbar, Alert, Typography } from "@mui/material";
import { authApi, systemSettingsApi } from "../api/client";
import { refreshSystemDefaults } from "../api/systemDefaults";
import {
  isSessionExpired,
  isSessionExpiringSoon,
  dispatchSessionExpired,
  clearSession,
  setSessionExpiry,
} from "../api/auth";

function hasCsrfCookie(): boolean {
  return document.cookie.split("; ").some((row) => row.startsWith("csrf_token="));
}

// Bug-7318: the CSRF cookie is deliberately NOT HttpOnly (JS must read it to
// echo it in request headers), so any client can set `csrf_token=anything` and
// satisfy a cookie-presence check. Presence of the cookie therefore proves
// nothing about a valid server session. The guard must call the server and let
// the backend confirm the session before rendering protected content, and it
// must fail CLOSED: until the server confirms the session, no protected child
// is rendered.
type SessionState = "pending" | "valid" | "invalid";

// Bug-7318 (system-admin lockout): a system-admin session carries
// tenant_id="__system__" in its JWT and NO stored tenant_id. `/users/me` is
// tenant-scoped (it resolves the tenant DB) and 500s for that session — see
// Login.tsx, which deliberately skips /users/me on system login. So the guard
// must pick a probe that matches the session type. Both probes are still
// SERVER-authoritative: the localStorage role only chooses WHICH authenticated
// endpoint to call; the server decides whether the session is valid. A forged
// cookie + forged role hits `/system/settings` (require_system_admin) and is
// rejected, so this does not reintroduce the cookie-trust bypass.
//   - system-admin session -> GET /system/settings (require_system_admin)
//   - tenant session        -> GET /users/me
function validateSession(isSystemAdminSession: boolean): Promise<unknown> {
  return isSystemAdminSession ? systemSettingsApi.list() : authApi.me();
}

export default function RequireAuth({
  children,
}: {
  children: React.ReactNode;
}) {
  const hasCookie = hasCsrfCookie();
  const t = useT();
  const [expiryWarning, setExpiryWarning] = useState(false);
  const warningShownRef = useRef(false);
  const refreshingRef = useRef(false);
  const role = safeLocalGet("user_role", "");
  const tenantId = safeLocalGet("tenant_id", "");
  // A system-admin session has role=system_admin and no stored tenant_id
  // (Login.tsx removes tenant_id on system login). Used only to choose the
  // server probe, never to make the auth decision itself.
  const isSystemAdminSession = role === "system_admin" && !tenantId;
  // Fail-closed: with a cookie, start "pending" and only render children once
  // the server confirms the session. Without a cookie there is nothing to
  // validate, so short-circuit to "invalid" on the first render (no server
  // call, no verifying-spinner flash) — the guard redirects immediately.
  const [session, setSession] = useState<SessionState>(
    hasCookie ? "pending" : "invalid",
  );

  useEffect(() => {
    if (!hasCookie) {
      setSession("invalid");
      return;
    }
    let cancelled = false;
    void validateSession(isSystemAdminSession)
      .then(() => {
        if (!cancelled) setSession("valid");
      })
      .catch(() => {
        // Any failure (401 from a forged/expired cookie, 403 from a role
        // mismatch, network error) fails closed. The axios 401 interceptor
        // already fires logout + session-expired; clear local state and
        // redirect regardless of status code.
        if (!cancelled) {
          clearSession();
          setSession("invalid");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [hasCookie, isSystemAdminSession]);

  useEffect(() => {
    if (session === "valid" && role === "system_admin") {
      void refreshSystemDefaults();
    }
  }, [session, role]);

  useEffect(() => {
    if (session !== "valid") return;
    const id = setInterval(() => {
      if (isSessionExpired()) {
        dispatchSessionExpired();
        clearInterval(id);
        return;
      }
      if (isSessionExpiringSoon(120) && !refreshingRef.current) {
        refreshingRef.current = true;
        void authApi
          .refresh()
          .then((result) => {
            if (result.expires_in) setSessionExpiry(result.expires_in);
            warningShownRef.current = false;
          })
          .catch(() => {
            if (!warningShownRef.current) {
              warningShownRef.current = true;
              setExpiryWarning(true);
            }
          })
          .finally(() => {
            refreshingRef.current = false;
          });
      }
    }, 15_000);
    return () => clearInterval(id);
  }, [session]);

  if (session === "invalid") {
    return <Navigate to="/login" replace />;
  }

  if (session === "pending") {
    return (
      <Box
        sx={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "60vh",
          gap: 2,
        }}
      >
        <CircularProgress size={28} />
        <Typography variant="body2" color="text.secondary">
          {t("session.verifying")}
        </Typography>
      </Box>
    );
  }

  return (
    <>
      {children}
      <Snackbar
        open={expiryWarning}
        autoHideDuration={10_000}
        onClose={() => setExpiryWarning(false)}
        anchorOrigin={{ vertical: "top", horizontal: "center" }}
      >
        <Alert severity="warning" onClose={() => setExpiryWarning(false)}>
          {t("session.expiringSoon")}
        </Alert>
      </Snackbar>
    </>
  );
}
