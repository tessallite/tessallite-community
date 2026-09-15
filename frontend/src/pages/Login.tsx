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

// Bug-9558: about line (version / deployment type / build commit). All three
// come from the build environment via `define` in vite.config.ts. Missing
// values degrade gracefully: each portion (including the version) is omitted
// entirely rather than rendering a malformed placeholder like "vunknown" —
// see Bug-9558's DR-02 closeout for why a bare "unknown" version used to
// render as "Tessallite vunknown" (the template's own literal " v" prefix
// had no space before an already-substituted word).
const BUILD_VERSION = import.meta.env.VITE_TESSALLITE_VERSION;
const DEPLOYMENT_TYPE = import.meta.env.VITE_DEPLOYMENT_TYPE;
const BUILD_COMMIT_HASH = import.meta.env.VITE_BUILD_COMMIT_HASH;

// Bug-9558 R2-07: a Docker image build-time label cannot express a runtime
// license fact — the same Community-release image is deployed with either a
// Community or an Enterprise licence, decided at INSTALL time and changeable
// afterward by uploading a new licence (see credentials-and-env.md's Licence
// Manager). `deploymentTypeOverride` carries the edition read from the
// deploy-time-generated `/edition.json` static artifact (see the fetch in the
// component below), taking precedence over the Docker build-arg default
// (VITE_DEPLOYMENT_TYPE) when present. VITE_DEPLOYMENT_TYPE alone remains
// correct for the two cases that ARE genuinely build/deploy-topology-fixed
// facts and never license-flip: "Dev Stack" and "Cloud Edition".
function aboutLine(
  t: (key: string, vars?: Record<string, string | number>) => string,
  deploymentTypeOverride?: string | null,
): string {
  const version = BUILD_VERSION?.trim();
  const deploymentType = (deploymentTypeOverride ?? DEPLOYMENT_TYPE)?.trim();
  // The about line shows the FIRST 13 characters of the commit hash
  // (the operator's example format [9138721837AH5] is 13 chars).
  const commitHash = BUILD_COMMIT_HASH?.trim().slice(0, 13);
  // The single login.about template carries the whole line; every portion is
  // folded into its placeholder value (leading space / "v" prefix / brackets
  // included) so an unset variable omits its portion cleanly instead of
  // leaving a stray literal character behind.
  return t("login.about", {
    deploymentType: deploymentType ? ` ${deploymentType}` : "",
    version: version ? ` v${version}` : "",
    commitHash: commitHash ? ` [${commitHash}]` : "",
  });
}

// Bug-9558 R2-07: format the /edition.json artifact's raw `edition` string
// (e.g. "community", "enterprise") into the same "<Name> Edition" form the
// build-time VITE_DEPLOYMENT_TYPE label already uses.
// Bug-9578 (R3-06, round-2 recheck): the previous version blindly title-cased
// whatever "edition" string /api/v1/edition returned, so an internal license
// state like "internal-unlimited" or "unactivated" rendered as an internal
// implementation detail ("Internal-unlimited Edition"). Known values are the
// finite domain the backend actually reports: shared/licensing/schema.py's
// KNOWN_EDITIONS (community, enterprise — the only values a real signed
// license carries) plus internal-unlimited (the dev/internal-unlimited
// manager, licensing_guard.py's _UnlimitedManager). Anything unrecognised
// gets no override — the build-time label stays in place rather than
// leaking a raw internal string to the login page.
const EDITION_LABELS: Record<string, string> = {
  community: "Community Edition",
  enterprise: "Enterprise Edition",
  "internal-unlimited": "Internal Edition",
};

function formatEditionLabel(edition: unknown): string | null {
  if (typeof edition !== "string") return null;
  return EDITION_LABELS[edition.trim().toLowerCase()] ?? null;
}

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
  // Bug-9558 R2-07: /edition.json is a plain static file (same-origin, no
  // auth) written by the deploy/install process — see
  // deploy/community/install.sh. It overrides the build-time
  // VITE_DEPLOYMENT_TYPE label for self-hosted installs where the same image
  // can carry either a Community or an Enterprise licence, decided at install
  // time and changeable afterward. Absent on Dev Stack / Cloud Edition builds
  // (nothing ever writes the file there), so this stays null and aboutLine()
  // falls back to the build-time label — never a broken or blank line.
  const [deploymentTypeOverride, setDeploymentTypeOverride] = useState<string | null>(null);

  useEffect(() => {
    ssoApi.getBackends(tenantId || undefined).then((data) => {
      setSsoEnabled({ saml: data.saml_enabled, oidc: data.oidc_enabled });
    }).catch((e) => console.warn("SSO backend check failed:", e));
  }, [tenantId]);

  useEffect(() => {
    let cancelled = false;
    fetch("/edition.json")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        setDeploymentTypeOverride(formatEditionLabel(data.edition));
      })
      .catch(() => {
        // Not present on this deployment (Dev Stack / Cloud Edition never
        // write it, and a fresh community install may not have run yet) —
        // stay on the build-time VITE_DEPLOYMENT_TYPE label.
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

          <Box mt={3} textAlign="center">
            <Typography
              variant="caption"
              color="text.secondary"
              data-testid="login-about"
            >
              {aboutLine(t, deploymentTypeOverride)}
            </Typography>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
