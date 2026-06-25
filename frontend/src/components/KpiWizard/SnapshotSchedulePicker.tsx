/**
 * Snapshot Schedule Picker — human-friendly controls for KPI snapshot
 * frequency and retention. Users never see raw cron expressions.
 */
import {
  Box,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { KpiWizardFormState } from "./types";
import { SNAPSHOT_FREQUENCY_OPTIONS } from "./types";

interface Props {
  form: KpiWizardFormState;
  onChange: (patch: Partial<KpiWizardFormState>) => void;
}

const MIN_RETENTION = 7;
const MAX_RETENTION = 365;

export default function SnapshotSchedulePicker({ form, onChange }: Props) {
  const t = useT();

  const handleRetentionChange = (value: string) => {
    const num = parseInt(value, 10);
    if (isNaN(num)) {
      onChange({ snapshot_retention: value });
      return;
    }
    const clamped = Math.max(MIN_RETENTION, Math.min(MAX_RETENTION, num));
    onChange({ snapshot_retention: String(clamped) });
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Typography variant="subtitle1" sx={{ mt: 1 }}>
        {t("kpis.wizard.v2.snapshot.sectionTitle")}
      </Typography>

      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.snapshot.frequencyLabel")}</InputLabel>
        <Select
          value={form.snapshot_frequency}
          label={t("kpis.wizard.v2.snapshot.frequencyLabel")}
          onChange={(e) => onChange({ snapshot_frequency: e.target.value })}
        >
          {SNAPSHOT_FREQUENCY_OPTIONS.map((opt) => (
            <MenuItem key={opt.value} value={opt.value}>
              {t(opt.labelKey)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {form.snapshot_frequency && (
        <TextField
          size="small"
          fullWidth
          type="number"
          label={t("kpis.wizard.v2.snapshot.retentionLabel")}
          value={form.snapshot_retention}
          onChange={(e) => handleRetentionChange(e.target.value)}
          helperText={t("kpis.wizard.v2.snapshot.retentionSuffix")}
          inputProps={{ min: MIN_RETENTION, max: MAX_RETENTION, step: 1 }}
        />
      )}
    </Box>
  );
}
