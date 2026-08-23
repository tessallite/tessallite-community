import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Typography,
} from "@mui/material";
import UploadFileIcon from "@mui/icons-material/UploadFile";
import { adminApi, type LicenseStatusDetail } from "../api/client";
import { useT } from "../i18n";

/**
 * System-admin License Manager: shows install state and lets an admin install or
 * replace the signed license live (Bug-5466). The backend verifies the signature
 * before writing and hot-reloads the manager. On a read-only deployment (e.g.
 * Cloud Run / Secret Manager) upload is unavailable and we say so.
 */
export default function LicenseManagerCard() {
  const t = useT();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  const status = useQuery({
    queryKey: ["license-status"],
    queryFn: adminApi.licenseStatus,
  });

  const install = useMutation({
    mutationFn: (doc: unknown) => adminApi.installLicense(doc),
    onSuccess: () => {
      setError(null);
      setOkMsg(t("license.manager.installed"));
      qc.invalidateQueries({ queryKey: ["license-status"] });
      qc.invalidateQueries({ queryKey: ["edition"] });
    },
    onError: (err: unknown) => {
      setOkMsg(null);
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      setError(resolveInstallError(detail, t) ?? t("license.manager.installFailed"));
    },
  });

  const uninstall = useMutation({
    mutationFn: () => adminApi.uninstallLicense(),
    onSuccess: () => {
      setError(null);
      setOkMsg(t("license.manager.uninstalled"));
      qc.invalidateQueries({ queryKey: ["license-status"] });
      qc.invalidateQueries({ queryKey: ["edition"] });
    },
    onError: () => {
      setOkMsg(null);
      setError(t("license.manager.uninstallFailed"));
    },
  });

  const onPickFile = async (file: File) => {
    setError(null);
    setOkMsg(null);
    try {
      const doc = JSON.parse(await file.text());
      install.mutate(doc);
    } catch {
      setError(t("license.manager.invalidJson"));
    }
  };

  const data = status.data;
  // Bug-8164 (F02): a persisted rejected licence arrives with a machine-readable
  // ``status.error_code``. Render its code-specific hint through the SAME single
  // mapping authority used for POST-rejection, falling back to the generic
  // state banner when the code is unknown/absent.
  const persistedHint = licenseCodeHint(data?.status?.error_code, t);

  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 0.5 }}>
        {t("license.manager.title")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("license.manager.subtitle")}
      </Typography>

      {status.isLoading && <CircularProgress size={20} />}

      {/* Bug-7470: a failed license-status read must be shown as an explicit,
          retryable error rather than rendering nothing (which reads as "no
          license status" and hides the network failure). */}
      {status.isError && !status.isLoading && (
        <Alert
          severity="error"
          data-testid="license-manager-load-error"
          action={
            <Button color="inherit" size="small" onClick={() => status.refetch()}>
              {t("common.retry")}
            </Button>
          }
        >
          {t("license.manager.loadError")}
        </Alert>
      )}

      {data && (
        <Stack spacing={1}>
          {/* Bug-7680: a licence document is installed but the verifier rejected
              it (expired / untrusted) -> status.license_state === "invalid".
              Without this the card shows "License installed: Yes" and reads as a
              healthy install, hiding that the licence is not actually active. */}
          {(isLicenseInvalid(data.status) || persistedHint) && (
            <Alert severity="error" data-testid="license-manager-invalid-banner">
              {persistedHint ?? t("license.manager.invalidBody")}
            </Alert>
          )}
          {isLicenseExpired(data.status) && !persistedHint && (
            <Alert severity="warning" data-testid="license-manager-expired-banner">
              {t("license.manager.expiredBody")}
            </Alert>
          )}
          <Row label={t("license.manager.edition")}>
            <Chip size="small" label={data.edition ?? t("license.manager.none")} />
          </Row>
          <Row label={t("license.manager.enforcement")}>
            <Chip
              size="small"
              color={data.enforcement_enabled ? "success" : "default"}
              variant={data.enforcement_enabled ? "filled" : "outlined"}
              label={data.enforcement_enabled ? t("common.enabled") : t("common.disabled")}
            />
          </Row>
          <Row label={t("license.manager.present")}>
            <Chip
              size="small"
              color={data.has_license ? "success" : "default"}
              variant={data.has_license ? "filled" : "outlined"}
              label={data.has_license ? t("common.yes") : t("common.no")}
            />
          </Row>

          <Divider sx={{ my: 1 }} />

          <Box>
            <input
              ref={fileRef}
              type="file"
              accept="application/json,.json"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onPickFile(f);
                e.target.value = "";
              }}
            />
            <Button
              variant="contained"
              startIcon={install.isPending ? <CircularProgress size={16} /> : <UploadFileIcon />}
              disabled={install.isPending}
              onClick={() => fileRef.current?.click()}
            >
              {t("license.manager.installButton")}
            </Button>
            <Button
              variant="outlined"
              size="small"
              color="warning"
              disabled={uninstall.isPending || !data.has_license}
              onClick={() => {
                if (!window.confirm(t("license.manager.uninstallConfirm"))) return;
                uninstall.mutate();
              }}
            >
              {uninstall.isPending ? <CircularProgress size={16} /> : t("license.manager.uninstallButton")}
            </Button>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              {t("license.manager.installHint")}
            </Typography>
          </Box>

          {okMsg && <Alert severity="success">{okMsg}</Alert>}
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      )}
    </Box>
  );
}

// Bug-8164: SINGLE mapping authority — a licence-failure ``error_code`` -> its
// localized, code-specific hint, or null when the code is unknown/absent. Used by
// BOTH the persisted GET /admin/license status banner AND the POST-rejection
// message, so there is exactly one code->text mapping. i18n keys are en-only;
// ``t()`` returns the key itself when there is no mapping, so an unknown code
// falls through to null (and the caller's generic fallback).
function licenseCodeHint(
  code: unknown,
  t: (key: string) => string,
): string | null {
  if (typeof code !== "string" || !code) return null;
  const key = `license.manager.errorCode.${code}`;
  const localized = t(key);
  return localized && localized !== key ? localized : null;
}

// Bug-8164: the install-reject response body is structured as
// ``{ detail: { error_code, message } }``. Prefer the code-specific hint (same
// authority as the persisted banner); fall back to the server message, and finally
// to a plain string detail (older/other error shapes).
function resolveInstallError(
  detail: unknown,
  t: (key: string) => string,
): string | null {
  if (typeof detail === "string") return detail || null;
  if (detail && typeof detail === "object") {
    const d = detail as { error_code?: unknown; message?: unknown };
    const hint = licenseCodeHint(d.error_code, t);
    if (hint) return hint;
    if (typeof d.message === "string" && d.message) return d.message;
  }
  return null;
}

// Bug-7680: the manager's ``status`` dict carries ``license_state: "invalid"``
// when an installed licence failed verification (see model-service
// _InvalidLicenseManager.status()). Bug-7851: ``license_state: "expired"`` when a
// once-valid licence has passed its ``expires_at``. Read defensively.
function isLicenseInvalid(status: LicenseStatusDetail | undefined): boolean {
  return status?.license_state === "invalid";
}

function isLicenseExpired(status: LicenseStatusDetail | undefined): boolean {
  return status?.license_state === "expired";
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
      <Typography variant="caption" color="text.secondary" sx={{ width: 110, fontWeight: 600 }}>
        {label}
      </Typography>
      {children}
    </Box>
  );
}
