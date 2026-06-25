import {
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";

export type RebuildMethod = "full" | "incremental";

interface Props {
  method: RebuildMethod;
  onMethodChange: (m: RebuildMethod) => void;
  incrementalColumn?: string;
  onIncrementalColumnChange?: (col: string) => void;
  lookbackDays?: number;
  onLookbackChange?: (days: number) => void;
  disabled?: boolean;
  size?: "small" | "medium";
}

export default function RebuildMethodPicker({
  method,
  onMethodChange,
  incrementalColumn = "",
  onIncrementalColumnChange,
  lookbackDays = 1,
  onLookbackChange,
  disabled,
  size = "small",
}: Props) {
  const t = useT();

  return (
    <Stack spacing={1.5} data-testid="rebuild-method-picker">
      <FormControl size={size} fullWidth disabled={disabled}>
        <InputLabel>{t("rebuildMethod.methodLabel")}</InputLabel>
        <Select
          value={method}
          label={t("rebuildMethod.methodLabel")}
          onChange={(e) => onMethodChange(e.target.value as RebuildMethod)}
          data-testid="rebuild-method-select"
        >
          <MenuItem value="full">{t("rebuildMethod.fullRebuild")}</MenuItem>
          <MenuItem value="incremental">{t("rebuildMethod.incrementalRebuild")}</MenuItem>
        </Select>
      </FormControl>

      {method === "incremental" && (
        <>
          <TextField
            label={t("rebuildMethod.dateColumnLabel")}
            size={size}
            fullWidth
            value={incrementalColumn}
            onChange={(e) => onIncrementalColumnChange?.(e.target.value)}
            disabled={disabled}
          />
          <TextField
            label={t("rebuildMethod.lookbackLabel")}
            type="number"
            size={size}
            fullWidth
            value={lookbackDays}
            onChange={(e) => onLookbackChange?.(Number(e.target.value))}
            disabled={disabled}
            inputProps={{ min: 0, max: 90 }}
          />
          <Typography variant="caption" color="text.secondary">
            {t("rebuildMethod.lookbackHelp")}
          </Typography>
        </>
      )}
    </Stack>
  );
}
