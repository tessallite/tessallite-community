import {
  Autocomplete,
  Checkbox,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { useT } from "../../i18n";
import type { Dimension } from "../../api/types";
import type { BusinessTimeWindow, TimeWindowPreset } from "../../api/types_domains/kpis";
import { TIME_WINDOW_PRESETS } from "./businessDefinition";

type Props = {
  timeWindow: BusinessTimeWindow;
  onChange: (tw: BusinessTimeWindow) => void;
  timeDimensions: Dimension[];
};

export function KpiTimeWindowPicker({ timeWindow, onChange, timeDimensions }: Props) {
  const t = useT();

  function patch(p: Partial<BusinessTimeWindow>) {
    onChange({ ...timeWindow, ...p });
  }

  const selectedDim = timeDimensions.find((d) => d.id === timeWindow.dimension_id) ?? null;
  const isCustom = timeWindow.preset === "custom_range";

  return (
    <Stack spacing={2}>
      <Typography variant="caption" color="text.secondary" display="block">
        {t("kpiBusiness.timeWindowSection")}
      </Typography>

      <Autocomplete
        size="small"
        options={timeDimensions}
        getOptionLabel={(d) => d.display_name || d.name}
        value={selectedDim}
        onChange={(_, v) => patch({ dimension_id: v?.id })}
        renderInput={(params) => (
          <TextField {...params} label={t("kpiBusiness.timeDimension")} />
        )}
        isOptionEqualToValue={(a, b) => a.id === b.id}
      />

      <FormControl size="small" fullWidth>
        <InputLabel>{t("kpiBusiness.timePreset")}</InputLabel>
        <Select
          label={t("kpiBusiness.timePreset")}
          value={timeWindow.preset ?? ""}
          onChange={(e) =>
            patch({
              preset: (e.target.value as TimeWindowPreset) || undefined,
              start: undefined,
              end: undefined,
            })
          }
        >
          <MenuItem value="">{t("kpiBusiness.noTimeWindow")}</MenuItem>
          {TIME_WINDOW_PRESETS.map((tw) => (
            <MenuItem key={tw.preset} value={tw.preset}>
              {t(tw.labelKey)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {isCustom && (
        <Stack direction="row" spacing={1}>
          <TextField
            size="small"
            type="date"
            label={t("kpiBusiness.startDate")}
            InputLabelProps={{ shrink: true }}
            value={timeWindow.start ?? ""}
            onChange={(e) => patch({ start: e.target.value || undefined })}
            sx={{ flex: 1 }}
          />
          <TextField
            size="small"
            type="date"
            label={t("kpiBusiness.endDate")}
            InputLabelProps={{ shrink: true }}
            value={timeWindow.end ?? ""}
            onChange={(e) => patch({ end: e.target.value || undefined })}
            sx={{ flex: 1 }}
          />
        </Stack>
      )}

      {timeWindow.preset && (
        <FormControlLabel
          control={
            <Checkbox
              size="small"
              checked={timeWindow.include_incomplete_period ?? false}
              onChange={(e) => patch({ include_incomplete_period: e.target.checked })}
            />
          }
          label={
            <Typography variant="body2">
              {t("kpiBusiness.includeIncompletePeriod")}
            </Typography>
          }
        />
      )}
    </Stack>
  );
}
