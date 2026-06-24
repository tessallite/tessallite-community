import { useEffect, useRef } from "react";
import { useT } from "../i18n";
import { safeLocalGet } from "../utils/safeLocalStorage";
import { Navigate } from "react-router-dom";
import { Snackbar, Alert } from "@mui/material";
import { useState } from "react";
import { refreshSystemDefaults } from "../api/systemDefaults";
import {
  isSessionExpired,
  isSessionExpiringSoon,
  dispatchSessionExpired,
} from "../api/auth";

function hasCsrfCookie(): boolean {
  return document.cookie.split("; ").some((row) => row.startsWith("csrf_token="));
}

export default function RequireAuth({
  children,
}: {
  children: React.ReactNode;
}) {
  const isAuthenticated = hasCsrfCookie();
  const t = useT();
  const [expiryWarning, setExpiryWarning] = useState(false);
  const warningShownRef = useRef(false);

  const role = safeLocalGet("user_role", "");
  useEffect(() => {
    if (isAuthenticated && role === "system_admin") {
      void refreshSystemDefaults();
    }
  }, [isAuthenticated, role]);

  useEffect(() => {
    if (!isAuthenticated) return;
    const id = setInterval(() => {
      if (isSessionExpired()) {
        dispatchSessionExpired();
        clearInterval(id);
        return;
      }
      if (isSessionExpiringSoon(60) && !warningShownRef.current) {
        warningShownRef.current = true;
        setExpiryWarning(true);
      }
    }, 15_000);
    return () => clearInterval(id);
  }, [isAuthenticated]);

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
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
