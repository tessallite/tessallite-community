import { Box, Chip, Stack, Typography } from "@mui/material";
import { useT } from "../../i18n";
import CheckCircleIcon from "@mui/icons-material/CheckCircleOutline";
import RemoveCircleIcon from "@mui/icons-material/RemoveCircleOutline";
import HighlightOffIcon from "@mui/icons-material/HighlightOff";

type Access = "edit" | "scoped" | "none";

/**
 * Render a small matrix showing which Configuration screens this user
 * will be able to access. Pure presentation — the backend re-checks
 * every API call.
 */
export default function EffectiveAccessPreview({ role }: { role: string }) {
  const t = useT();

  const ROWS: Array<{ label: string; byRole: Record<string, Access>; hint: string }> = [
    {
      label: t("effectiveAccess.systemConfig"),
      byRole: { system_admin: "edit", tenant_admin: "none", modeler: "none", member: "none", viewer: "none" },
      hint: t("effectiveAccess.systemConfigHint"),
    },
    {
      label: t("effectiveAccess.tenantConfig"),
      byRole: { system_admin: "edit", tenant_admin: "edit", modeler: "none", member: "none", viewer: "none" },
      hint: t("effectiveAccess.tenantConfigHint"),
    },
    {
      label: t("effectiveAccess.projectConfig"),
      byRole: { system_admin: "edit", tenant_admin: "edit", modeler: "scoped", member: "scoped", viewer: "none" },
      hint: t("effectiveAccess.projectConfigHint"),
    },
    {
      label: t("effectiveAccess.modelConfig"),
      byRole: { system_admin: "edit", tenant_admin: "edit", modeler: "scoped", member: "scoped", viewer: "none" },
      hint: t("effectiveAccess.modelConfigHint"),
    },
  ];

  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
        {t("effectiveAccess.title")}
      </Typography>
      <Stack spacing={0.5}>
        {ROWS.map((row) => {
          const access = row.byRole[role] ?? "none";
          return (
            <Box
              key={row.label}
              sx={{
                display: "grid",
                gridTemplateColumns: "1fr auto",
                alignItems: "center",
                px: 1,
                py: 0.5,
                borderRadius: 1,
                bgcolor: access === "none" ? "action.hover" : "transparent",
                border: 1,
                borderColor: "divider",
              }}
            >
              <Box>
                <Typography variant="body2" sx={{ fontWeight: 500 }}>
                  {row.label}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {row.hint}
                </Typography>
              </Box>
              <AccessChip access={access} />
            </Box>
          );
        })}
      </Stack>
    </Box>
  );
}

function AccessChip({ access }: { access: Access }) {
  const t = useT();
  if (access === "edit") {
    return (
      <Chip
        size="small"
        icon={<CheckCircleIcon fontSize="small" />}
        label={t("effectiveAccess.edit")}
        color="success"
        variant="outlined"
      />
    );
  }
  if (access === "scoped") {
    return (
      <Chip
        size="small"
        icon={<RemoveCircleIcon fontSize="small" />}
        label={t("effectiveAccess.editAssigned")}
        color="info"
        variant="outlined"
      />
    );
  }
  return (
    <Chip
      size="small"
      icon={<HighlightOffIcon fontSize="small" />}
      label={t("effectiveAccess.noAccess")}
      variant="outlined"
    />
  );
}
