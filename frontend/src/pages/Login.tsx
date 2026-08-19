import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Divider,
  Link,
  TextField,
  Typography,
} from "@mui/material";
import LockOpenIcon from "@mui/icons-material/LockOpen";
import PlayCircleOutlineIcon from "@mui/icons-material/PlayCircleOutline";
import { authApi, ssoApi } from "../api/client";
import { setSessionExpiry } from "../api/auth";
import { BRANDING_CHANGED_EVENT } from "../utils/brandingEvents";
import { useT } from "../i18n";

const DEMO_ENABLED = import.meta.env.VITE_ENABLE_DEMO_LOGIN === "true";
const DEMO_TENANT_SLUG = DEMO_ENABLED ? (import.meta.env.VITE_DEMO_TENANT_SLUG ?? "") : "";
const DEMO_EMAIL = DEMO_ENABLED ? (import.meta.env.VITE_DEMO_EMAIL ?? "") : "";
const DEMO_PASSWORD = DEMO_ENABLED ? (import.meta.env.VITE_DEMO_PASSWORD ?? "") : "";

export default function Login() {
  const t = useT();
  const navigate = useNavigate();
  const [isSystemLogin, setIsSystemLogin] = useState(false);
  const [tenantId, setTenantId] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ssoEnabled, setSsoEnabled] = useState<{
    saml: boolean;
    oidc: boolean;
  }>({ saml: false, oidc: false });

  useEffect(() => {
    ssoApi.getBackends(tenantId || undefined).then((data) => {
      setSsoEnabled({ saml: data.saml_enabled, oidc: data.oidc_enabled });
    }).catch((e) => console.warn("SSO backend check failed:", e));
  }, [tenantId]);

  async function signInTenant(slug: string, emailAddr: string, pw: string) {
    const result = await authApi.login({ tenant_id: slug, email: emailAddr, password: pw });
    localStorage.setItem("tenant_id", slug);
    window.dispatchEvent(new Event(BRANDING_CHANGED_EVENT));
    if (result.expires_in) setSessionExpiry(result.expires_in);
    // Bug-837 fix: resolve role from /users/me (server authority) instead of
    // trusting the login response role directly into localStorage.
    try {
      const me = await authApi.me();
      if (me.role) {
        localStorage.setItem("user_role", me.role);
      } else {
        localStorage.removeItem("user_role");
      }
    } catch {
      // Fallback to login response if /me fails (should not happen)
      if (result.role) {
        localStorage.setItem("user_role", result.role);
      } else {
        localStorage.removeItem("user_role");
      }
    }
    navigate("/", { replace: true });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      if (isSystemLogin) {
        const result = await authApi.systemLogin({ email, password });
        // System login JWT carries role=system_admin; the /users/me endpoint
        // requires a tenant context so we set the role from the JWT claim
        // directly (system_admin is always system_admin).
        localStorage.setItem("user_role", "system_admin");
        localStorage.removeItem("tenant_id");
        if (result.expires_in) setSessionExpiry(result.expires_in);
        navigate("/", { replace: true });
      } else {
        await signInTenant(tenantId, email, password);
      }
    } catch {
      setError(
        isSystemLogin
          ? t("login.systemLoginFailed")
          : t("login.tenantLoginFailed")
      );
    } finally {
      setLoading(false);
    }
  }

  async function handleDemoLogin() {
    setLoading(true);
    setError(null);
    try {
      await signInTenant(DEMO_TENANT_SLUG, DEMO_EMAIL, DEMO_PASSWORD);
    } catch {
      setError(t("login.demoLoginFailed", { slug: DEMO_TENANT_SLUG }));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Box
      sx={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        bgcolor: "background.default",
      }}
    >
      <Card sx={{ width: 380 }}>
        <CardContent sx={{ p: 4 }}>
          <Box display="flex" alignItems="center" gap={1} mb={1}>
            <img src="/favicon.png" alt={t("login.logoAlt")} width={32} height={32} />
            <Typography variant="h5" fontWeight={700}>
              {t("login.appName")}
            </Typography>
          </Box>
          <Typography variant="body2" color="text.secondary" mb={3}>
            {isSystemLogin ? t("login.systemAdminSubtitle") : t("login.tenantSubtitle")}
          </Typography>

          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          <Box component="form" onSubmit={handleSubmit}>
            {!isSystemLogin && (
              <TextField
                label={t("login.workspaceLabel")}
                fullWidth
                margin="normal"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                required
                autoFocus
              />
            )}
            <TextField
              label={t("login.emailLabel")}
              type="email"
              fullWidth
              margin="normal"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoFocus={isSystemLogin}
            />
            <TextField
              label={t("login.passwordLabel")}
              type="password"
              fullWidth
              margin="normal"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <Button
              type="submit"
              variant="contained"
              fullWidth
              size="large"
              disabled={loading}
              sx={{ mt: 2 }}
            >
              {loading ? <CircularProgress size={22} /> : t("login.signInButton")}
            </Button>
          </Box>

          {!isSystemLogin && (ssoEnabled.saml || ssoEnabled.oidc) && tenantId && (
            <>
              <Divider sx={{ my: 2 }}>{t("login.orDivider")}</Divider>
              {ssoEnabled.saml && (
                <Button
                  variant="outlined"
                  fullWidth
                  size="large"
                  startIcon={<LockOpenIcon />}
                  disabled={loading}
                  sx={{ mb: 1 }}
                  onClick={() => {
                    window.location.assign(ssoApi.samlLoginUrl(tenantId));
                  }}
                >
                  {t("login.signInWithSaml")}
                </Button>
              )}
              {ssoEnabled.oidc && (
                <Button
                  variant="outlined"
                  fullWidth
                  size="large"
                  startIcon={<LockOpenIcon />}
                  disabled={loading}
                  onClick={() => {
                    window.location.assign(ssoApi.oidcLoginUrl(tenantId));
                  }}
                >
                  {t("login.signInWithOidc")}
                </Button>
              )}
            </>
          )}

          {!isSystemLogin && DEMO_ENABLED && (
            <>
              <Divider sx={{ my: 2 }}>{t("login.orDivider")}</Divider>
              <Button
                variant="outlined"
                fullWidth
                size="large"
                startIcon={<PlayCircleOutlineIcon />}
                disabled={loading}
                onClick={handleDemoLogin}
              >
                {t("login.signInToDemo", { slug: DEMO_TENANT_SLUG })}
              </Button>
            </>
          )}

          <Box mt={2} textAlign="center">
            <Link
              component="button"
              variant="body2"
              onClick={() => {
                setIsSystemLogin(!isSystemLogin);
                setError(null);
              }}
            >
              {isSystemLogin
                ? t("login.backToTenantLogin")
                : t("login.systemAdminLogin")}
            </Link>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
