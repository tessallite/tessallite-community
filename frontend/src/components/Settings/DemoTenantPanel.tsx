import { useState } from "react";
import {
  Box,
  Divider,
  IconButton,
  Paper,
  Tooltip,
  Typography,
  useTheme,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import CheckIcon from "@mui/icons-material/Check";
import ScienceIcon from "@mui/icons-material/Science";
import { useT } from "../../i18n";
import DemoReseedButton from "../Admin/DemoReseedButton";

/**
 * Project-setup-drawer panel for the standalone `demo` tenant. Shows the fixed
 * demo sign-in credentials as an elegant card and hosts the reseed control.
 *
 * The demo tenant is a throwaway showcase, separate from every real tenant, so
 * its credentials are well-known and safe to display. The values below mirror
 * the seed fixture `deploy/demo-tenant/seed_demo_project.py` (the source of
 * truth) — keep them in step if that script changes.
 */
const DEMO_TENANT = {
  workspace: "demo",
  email: "admin@demo.com",
  password: "demo",
  project: "Project Demo",
  models: "modely, onboarding, inventory",
} as const;

function CredentialRow({
  label,
  value,
  mono = true,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  const t = useT();
  const theme = useTheme();
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — non-fatal */
    }
  };

  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 2,
        py: 1.25,
      }}
    >
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ width: 120, flexShrink: 0, fontWeight: 600 }}
      >
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{
          flexGrow: 1,
          fontFamily: mono ? "monospace" : undefined,
          fontSize: mono ? "0.85rem" : undefined,
          color: "text.primary",
        }}
      >
        {value}
      </Typography>
      <Tooltip title={copied ? t("demoTenant.copied") : t("demoTenant.copy")}>
        <IconButton
          size="small"
          onClick={copy}
          aria-label={`${t("demoTenant.copy")} ${label}`}
          sx={{ color: copied ? theme.palette.success.main : "text.disabled" }}
        >
          {copied ? (
            <CheckIcon sx={{ fontSize: 16 }} />
          ) : (
            <ContentCopyIcon sx={{ fontSize: 16 }} />
          )}
        </IconButton>
      </Tooltip>
    </Box>
  );
}

export default function DemoTenantPanel() {
  const t = useT();
  const theme = useTheme();

  return (
    <Box sx={{ p: 2, maxWidth: 560 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        {t("demoTenant.title")}
      </Typography>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("demoTenant.description")}
      </Typography>

      {/* Credentials card — same border/radius language as the model cards. */}
      <Paper
        elevation={0}
        sx={{
          border: `1px solid ${theme.palette.grey[300]}`,
          borderRadius: 1,
          overflow: "hidden",
        }}
      >
        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 1.5,
            px: 2,
            py: 1.5,
            bgcolor: theme.palette.grey[50],
            borderBottom: `1px solid ${theme.palette.grey[200]}`,
          }}
        >
          <ScienceIcon fontSize="small" sx={{ color: "primary.main" }} />
          <Typography variant="subtitle2" fontWeight={700}>
            {t("demoTenant.credentialsTitle")}
          </Typography>
        </Box>

        <Box sx={{ px: 2, py: 0.5 }}>
          <CredentialRow label={t("demoTenant.workspace")} value={DEMO_TENANT.workspace} />
          <Divider />
          <CredentialRow label={t("demoTenant.email")} value={DEMO_TENANT.email} />
          <Divider />
          <CredentialRow label={t("demoTenant.password")} value={DEMO_TENANT.password} />
          <Divider />
          <CredentialRow label={t("demoTenant.project")} value={DEMO_TENANT.project} mono={false} />
          <Divider />
          <CredentialRow label={t("demoTenant.models")} value={DEMO_TENANT.models} mono={false} />
        </Box>
      </Paper>

      {/* Reseed action. */}
      <Box sx={{ mt: 4 }}>
        <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
          {t("demoTenant.reseedTitle")}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t("demoTenant.reseedDescription")}
        </Typography>
        <DemoReseedButton />
      </Box>
    </Box>
  );
}
