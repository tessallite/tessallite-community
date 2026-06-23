import { FormControl, InputLabel, MenuItem, Select } from "@mui/material";
import { useT } from "../../i18n";

export interface FrequencyPreset {
  value: string;
  cron: string | null;
}

const PRESET_VALUES: Array<{ value: string; cron: string | null; key: string }> = [
  { value: "manual", key: "frequency.manualOnly", cron: null },
  { value: "every_6h", key: "frequency.every6h", cron: "0 */6 * * *" },
  { value: "every_12h", key: "frequency.every12h", cron: "0 */12 * * *" },
  { value: "daily_2am", key: "frequency.daily2am", cron: "0 2 * * *" },
  { value: "daily_5am", key: "frequency.daily5am", cron: "0 5 * * *" },
  { value: "weekly_mon", key: "frequency.weeklyMon", cron: "0 5 * * 1" },
];

export function cronToPreset(cron: string | null): string {
  if (!cron) return "manual";
  return PRESET_VALUES.find((p) => p.cron === cron)?.value ?? "daily_2am";
}

export function presetToCron(preset: string): string | null {
  return PRESET_VALUES.find((p) => p.value === preset)?.cron ?? null;
}

export function cronToLabel(cron: string | null): string {
  // Falls back to the cron expression itself if no match — no i18n hook needed in a pure function.
  if (!cron) return "Manual only";
  return PRESET_VALUES.find((p) => p.cron === cron)?.value ?? cron;
}

interface Props {
  value: string;
  onChange: (preset: string) => void;
  disabled?: boolean;
  size?: "small" | "medium";
}

export default function FrequencyPicker({ value, onChange, disabled, size = "small" }: Props) {
  const t = useT();

  return (
    <FormControl size={size} fullWidth disabled={disabled} data-testid="frequency-picker">
      <InputLabel>{t("frequency.rebuildFrequencyLabel")}</InputLabel>
      <Select
        value={value}
        label={t("frequency.rebuildFrequencyLabel")}
        onChange={(e) => onChange(e.target.value)}
        data-testid="frequency-picker-select"
      >
        {PRESET_VALUES.map((p) => (
          <MenuItem key={p.value} value={p.value}>
            {t(p.key as any)}
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  );
}
