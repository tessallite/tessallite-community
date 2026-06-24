/**
 * KPI Wizard Step 2 — Target configuration and direction.
 *
 * The user selects target type (none/static/measure/prior_period/expression),
 * configures the target value, and chooses the performance direction.
 */
import {
  Box,
  FormControl,
  FormHelperText,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { Measure } from "../../api/types";
import type { KpiWizardFormState } from "./types";
import { DIRECTION_OPTIONS, TARGET_TYPE_OPTIONS } from "./types";

interface Props {
  form: KpiWizardFormState;
  onChange: (patch: Partial<KpiWizardFormState>) => void;
  measures: Measure[];
}

export default function KpiWizardStep2TargetDirection({ form, onChange, measures }: Props) {
  const t = useT();

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
      <Typography variant="h6">{t("kpis.wizard.v2.targetTitle")}</Typography>
      <Typography variant="body2" color="text.secondary">
        {t("kpis.wizard.v2.targetSubtitle")}
      </Typography>

      {/* Target type selector */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.targetType")}</InputLabel>
        <Select
          value={form.target_type}
          label={t("kpis.wizard.v2.targetType")}
          onChange={(e) => onChange({ target_type: e.target.value as KpiWizardFormState["target_type"] })}
        >
          {TARGET_TYPE_OPTIONS.map((opt) => (
            <MenuItem key={opt.value} value={opt.value}>
              {t(opt.labelKey)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {/* Target-type-specific inputs */}
      {form.target_type === "static" && (
        <TextField
          size="small"
          fullWidth
          label={t("kpis.wizard.v2.targetValue")}
          type="number"
          value={form.target_value}
          onChange={(e) => onChange({ target_value: e.target.value })}
        />
      )}

      {form.target_type === "measure" && (
        <FormControl fullWidth size="small">
          <InputLabel>{t("kpis.wizard.v2.targetMeasure")}</InputLabel>
          <Select
            value={form.target_measure_id}
            label={t("kpis.wizard.v2.targetMeasure")}
            onChange={(e) => onChange({ target_measure_id: e.target.value })}
          >
            <MenuItem value="">{t("kpis.none")}</MenuItem>
            {measures.map((m) => (
              <MenuItem key={m.id} value={m.id}>
                {m.display_name || m.name}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      {form.target_type === "prior_period" && (
        <FormControl fullWidth size="small">
          <InputLabel>{t("kpis.wizard.v2.periodGrain")}</InputLabel>
          <Select
            value={form.target_period || form.trend_period}
            label={t("kpis.wizard.v2.periodGrain")}
            onChange={(e) => onChange({ target_period: e.target.value })}
          >
            <MenuItem value="day">{t("kpis.wizard.v2.periodDay")}</MenuItem>
            <MenuItem value="week">{t("kpis.wizard.v2.periodWeek")}</MenuItem>
            <MenuItem value="month">{t("kpis.wizard.v2.periodMonth")}</MenuItem>
            <MenuItem value="quarter">{t("kpis.wizard.v2.periodQuarter")}</MenuItem>
            <MenuItem value="year">{t("kpis.wizard.v2.periodYear")}</MenuItem>
          </Select>
        </FormControl>
      )}

      {form.target_type === "expression" && (
        <TextField
          size="small"
          fullWidth
          multiline
          minRows={2}
          maxRows={4}
          label={t("kpis.wizard.v2.targetExpression")}
          value={form.target_expression}
          onChange={(e) => onChange({ target_expression: e.target.value })}
          sx={{ fontFamily: "monospace", fontSize: 13 }}
        />
      )}

      {/* Direction selector */}
      <Box sx={{ mt: 1 }}>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>
          {t("kpis.wizard.v2.direction")}
        </Typography>
        <FormControl fullWidth size="small">
          <Select
            value={form.direction}
            onChange={(e) => onChange({ direction: e.target.value as KpiWizardFormState["direction"] })}
          >
            {DIRECTION_OPTIONS.map((opt) => (
              <MenuItem key={opt.value} value={opt.value}>
                <Box>
                  <Typography variant="body2">{t(opt.labelKey)}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t(opt.descKey)}
                  </Typography>
                </Box>
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>
    </Box>
  );
}
