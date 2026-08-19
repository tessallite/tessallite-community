import { Box, Chip } from "@mui/material";
import { useEdition, useLimits } from "../api/hooks";
import { useT } from "../i18n";

/**
 * Edition + model-count chips for the main top bar (green AppBar). Read-only —
 * reflects the license manager's /edition + /limits. White text so it reads on the
 * green header. Renders nothing until the edition resolves. This is the single home
 * for these chips (do not duplicate them in page headers).
 */
const HEADER_CHIP_SX = {
  color: "common.white",
  borderColor: "rgba(255,255,255,0.7)",
  "& .MuiChip-label": { color: "common.white" },
} as const;

export default function EditionBadge() {
  const t = useT();
  const { data: edition } = useEdition();
  const { data: limits } = useLimits();

  const name = edition?.edition;
  if (!name) return null;

  const label =
    name === "community"
      ? t("edition.community")
      : name === "enterprise"
        ? t("edition.enterprise")
        : name === "unactivated"
          ? t("edition.unactivated")
          : name === "internal-unlimited"
            ? t("edition.internal-unlimited")
            : name;

  const ent = (limits?.entitlements ?? {}) as Record<string, unknown>;
  const usedModels = limits?.usage?.models;
  const maxModels = ent.models;
  const modelsLabel =
    maxModels != null
      ? `${t("edition.modelsLabel")}: ${usedModels ?? "-"}/${maxModels}`
      : usedModels != null
        ? `${t("edition.modelsLabel")}: ${usedModels}`
        : null;

  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 1, mr: 1 }}>
      <Chip
        size="small"
        variant="outlined"
        label={label}
        sx={HEADER_CHIP_SX}
        data-testid="edition-badge"
      />
      {modelsLabel && (
        <Chip
          size="small"
          variant="outlined"
          label={modelsLabel}
          sx={HEADER_CHIP_SX}
          data-testid="edition-models"
        />
      )}
    </Box>
  );
}
