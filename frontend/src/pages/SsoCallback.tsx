import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Box, CircularProgress, Typography } from "@mui/material";
import { useT } from "../i18n";
import { authApi } from "../api/client";

export default function SsoCallback() {
  const t = useT();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const tenant = params.get("tenant_id");
    if (tenant) localStorage.setItem("tenant_id", tenant);

    // Bug-836/837 fix: resolve role from server via /users/me instead of
    // trusting URL parameters. The JWT cookie was already set by the SSO
    // redirect, so /users/me will authenticate using it.
    authApi
      .me()
      .then((user) => {
        if (user.role) {
          localStorage.setItem("user_role", user.role);
        } else {
          localStorage.removeItem("user_role");
        }
        navigate("/", { replace: true });
      })
      .catch(() => {
        // If /me fails (network, expired cookie), clear and redirect to login
        localStorage.removeItem("user_role");
        setError(t("ssoCallback.failedToResolveUser"));
        setTimeout(() => navigate("/login", { replace: true }), 2000);
      });
  }, [params, navigate, t]);

  return (
    <Box
      sx={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <CircularProgress size={32} />
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
        {error ?? t("ssoCallback.completingSignIn")}
      </Typography>
    </Box>
  );
}
