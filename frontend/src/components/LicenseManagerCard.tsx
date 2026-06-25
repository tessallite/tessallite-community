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
import { adminApi } from "../api/client";
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
      qc.invalidateQueries({ queryKey: ["limits"] });
    },
    onError: (err: unknown) => {
      setOkMsg(null);
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? t("license.manager.installFailed"));
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

  return (
    <Box>
      <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 0.5 }}>
        {t("license.manager.title")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("license.manager.subtitle")}
      </Typography>

      {status.isLoading && <CircularProgress size={20} />}

      {data && (
        <Stack spacing={1}>
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
