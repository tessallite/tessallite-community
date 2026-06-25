import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useT } from "../../../../i18n";
import {
  IconButton,
  MenuItem,
  MenuList,
  Paper,
  Popover,
  Tooltip,
  Typography,
} from "@mui/material";
import SettingsIcon from "@mui/icons-material/Settings";
import CheckIcon from "@mui/icons-material/Check";
import { measuresApi } from "../../../../api/client";
import {
  MEASURE_FORMAT_LABELS,
  MEASURE_FORMAT_TOKENS,
} from "../../../../api/measureFormat";
import type { Measure, MeasureFormatToken } from "../../../../api/types";

type Props = {
  projectId: string;
  modelId: string;
  measure: Measure;
};

export default function FormatPopover({ projectId, modelId, measure }: Props) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [saving, setSaving] = useState(false);
  const qc = useQueryClient();

  const current = (measure.format ?? null) as MeasureFormatToken | null;

  async function pick(token: MeasureFormatToken | null) {
    if (token === current) {
      setAnchor(null);
      return;
    }
    setSaving(true);
    try {
      await measuresApi.update(projectId, modelId, measure.id, {
        format: token,
      });
      await qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
    } finally {
      setSaving(false);
      setAnchor(null);
    }
  }

  return (
    <>
      <Tooltip title={t("formatPopover.tooltipTitle")}>
        <span>
          <IconButton
            size="small"
            aria-label={t("formatPopover.ariaLabel")}
            onClick={(e) => setAnchor(e.currentTarget)}
            disabled={saving}
          >
            <SettingsIcon fontSize="small" />
          </IconButton>
        </span>
      </Tooltip>
      <Popover
        open={Boolean(anchor)}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
      >
        <Paper sx={{ minWidth: 220 }}>
          <Typography
            variant="caption"
            sx={{ px: 2, pt: 1, color: "text.secondary" }}
          >
            {t("formatPopover.label")}
          </Typography>
          <MenuList dense>
            <MenuItem onClick={() => pick(null)} selected={current === null}>
              {current === null && (
                <CheckIcon fontSize="inherit" sx={{ mr: 1 }} />
              )}
              <em>{t("formatPopover.noneRaw")}</em>
            </MenuItem>
            {MEASURE_FORMAT_TOKENS.map((tok) => (
              <MenuItem
                key={tok}
                onClick={() => pick(tok)}
                selected={current === tok}
              >
                {current === tok && (
                  <CheckIcon fontSize="inherit" sx={{ mr: 1 }} />
                )}
                {t(MEASURE_FORMAT_LABELS[tok])}
              </MenuItem>
            ))}
          </MenuList>
        </Paper>
      </Popover>
    </>
  );
}
