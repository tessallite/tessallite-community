/**
 * KPI Wizard Step 4 — Name, format, and display options.
 */
import {
  Box,
  Divider,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { Dimension, Kpi } from "../../api/types";
import type { KpiWizardFormState } from "./types";
import { FORMAT_TOKEN_OPTIONS, PRESENTATION_TYPE_OPTIONS } from "./types";
import SnapshotSchedulePicker from "./SnapshotSchedulePicker";

interface Props {
  form: KpiWizardFormState;
  onChange: (patch: Partial<KpiWizardFormState>) => void;
  kpis: Kpi[];
  dimensions: Dimension[];
}

export default function KpiWizardStep4Display({ form, onChange, kpis, dimensions }: Props) {
  const t = useT();

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
      <Typography variant="h6">{t("kpis.wizard.v2.displayTitle")}</Typography>
      <Typography variant="body2" color="text.secondary">
        {t("kpis.wizard.v2.displaySubtitle")}
      </Typography>

      {/* Name fields */}
      <TextField
        size="small"
        fullWidth
        label={t("kpis.name")}
        value={form.name}
        onChange={(e) => onChange({ name: e.target.value })}
        required
      />
      <TextField
        size="small"
        fullWidth
        label={t("kpis.displayName")}
        value={form.display_name}
        onChange={(e) => onChange({ display_name: e.target.value })}
      />
      <TextField
        size="small"
        fullWidth
        label={t("kpis.description")}
        value={form.description}
        onChange={(e) => onChange({ description: e.target.value })}
        multiline
        minRows={2}
        maxRows={4}
      />
      <TextField
        size="small"
        fullWidth
        label={t("kpis.displayFolder")}
        value={form.display_folder}
        onChange={(e) => onChange({ display_folder: e.target.value })}
        helperText={t("kpis.wizard.v2.unitLabelHelp")}
      />

      {/* Format token */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.formatToken")}</InputLabel>
        <Select
          value={form.format_token}
          label={t("kpis.wizard.v2.formatToken")}
          onChange={(e) => onChange({ format_token: e.target.value as KpiWizardFormState["format_token"] })}
        >
          <MenuItem value="">{t("kpis.none")}</MenuItem>
          {FORMAT_TOKEN_OPTIONS.map((opt) => (
            <MenuItem key={opt.value} value={opt.value}>
              {t(opt.labelKey)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {form.format_token === "custom" && (
        <TextField
          size="small"
          fullWidth
          label={t("kpis.wizard.v2.formatCustom")}
          value={form.format_custom}
          onChange={(e) => onChange({ format_custom: e.target.value })}
          placeholder="0.00%"
        />
      )}

      {/* Visual presentation type */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.visualType")}</InputLabel>
        <Select
          value={form.presentation_type}
          label={t("kpis.wizard.v2.visualType")}
          onChange={(e) => onChange({ presentation_type: e.target.value as KpiWizardFormState["presentation_type"] })}
        >
          <MenuItem value="">{t("kpis.none")}</MenuItem>
          {PRESENTATION_TYPE_OPTIONS.map((opt) => (
            <MenuItem key={opt.value} value={opt.value} sx={{ display: "block", py: 0.75 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {t(opt.labelKey)}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "normal" }}>
                {t(opt.descKey)}
              </Typography>
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      <TextField
        size="small"
        fullWidth
        label={t("kpis.wizard.v2.unitLabel")}
        value={form.unit_label}
        onChange={(e) => onChange({ unit_label: e.target.value })}
        helperText={t("kpis.wizard.v2.unitLabelHelp")}
      />

      <TextField
        size="small"
        fullWidth
        label={t("kpis.wizard.v2.nullDisplay")}
        value={form.null_display_value}
        onChange={(e) => onChange({ null_display_value: e.target.value })}
        helperText={t("kpis.wizard.v2.nullDisplayHelp")}
      />

      {/* Trend settings */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.trendPeriod")}</InputLabel>
        <Select
          value={form.trend_period}
          label={t("kpis.wizard.v2.trendPeriod")}
          onChange={(e) => onChange({ trend_period: e.target.value })}
        >
          <MenuItem value="day">{t("kpis.wizard.v2.periodDay")}</MenuItem>
          <MenuItem value="week">{t("kpis.wizard.v2.periodWeek")}</MenuItem>
          <MenuItem value="month">{t("kpis.wizard.v2.periodMonth")}</MenuItem>
          <MenuItem value="quarter">{t("kpis.wizard.v2.periodQuarter")}</MenuItem>
          <MenuItem value="year">{t("kpis.wizard.v2.periodYear")}</MenuItem>
        </Select>
      </FormControl>

      {/* Indicator type */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.indicatorType")}</InputLabel>
        <Select
          value={form.indicator_type}
          label={t("kpis.wizard.v2.indicatorType")}
          onChange={(e) => onChange({ indicator_type: e.target.value as KpiWizardFormState["indicator_type"] })}
        >
          <MenuItem value="none">{t("kpis.wizard.v2.indicatorNone")}</MenuItem>
          <MenuItem value="leading">{t("kpis.wizard.v2.indicatorLeading")}</MenuItem>
          <MenuItem value="lagging">{t("kpis.wizard.v2.indicatorLagging")}</MenuItem>
        </Select>
      </FormControl>

      {/* Time dimension */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.timeDimension")}</InputLabel>
        <Select
          value={form.time_dimension_id}
          label={t("kpis.wizard.v2.timeDimension")}
          onChange={(e) => onChange({ time_dimension_id: e.target.value })}
        >
          <MenuItem value="">{t("kpis.none")}</MenuItem>
          {dimensions
            .filter((d) => d.is_time_dim || d.data_type === "date" || d.data_type === "datetime")
            .map((d) => (
              <MenuItem key={d.id} value={d.id}>
                {d.display_name || d.name}
              </MenuItem>
            ))}
        </Select>
      </FormControl>

      {/* Aggregation mode */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.aggMode")}</InputLabel>
        <Select
          value={form.calc_agg_mode}
          label={t("kpis.wizard.v2.aggMode")}
          onChange={(e) => onChange({ calc_agg_mode: e.target.value as KpiWizardFormState["calc_agg_mode"] })}
        >
          <MenuItem value="automatic">{t("kpis.wizard.v2.aggAutomatic")}</MenuItem>
          <MenuItem value="aggregate_first">{t("kpis.wizard.v2.aggAggregateFirst")}</MenuItem>
          <MenuItem value="row_first">{t("kpis.wizard.v2.aggRowFirst")}</MenuItem>
          <MenuItem value="pre_aggregated">{t("kpis.wizard.v2.aggPreAggregated")}</MenuItem>
          <MenuItem value="aggregate_of_aggregate">{t("kpis.wizard.v2.aggAggOfAgg")}</MenuItem>
        </Select>
      </FormControl>

      {/* Aggregate-of-aggregate fields (visible only when that mode is selected) */}
      {form.calc_agg_mode === "aggregate_of_aggregate" && (
        <>
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.innerAgg")}</InputLabel>
            <Select
              value={form.inner_agg}
              label={t("kpis.wizard.v2.innerAgg")}
              onChange={(e) => onChange({ inner_agg: e.target.value })}
            >
              <MenuItem value="sum">{t("kpis.wizard.v2.aggSum")}</MenuItem>
              <MenuItem value="avg">{t("kpis.wizard.v2.aggAvg")}</MenuItem>
              <MenuItem value="min">{t("kpis.wizard.v2.aggMin")}</MenuItem>
              <MenuItem value="max">{t("kpis.wizard.v2.aggMax")}</MenuItem>
              <MenuItem value="count">{t("kpis.wizard.v2.aggCount")}</MenuItem>
              <MenuItem value="count_distinct">{t("kpis.wizard.v2.aggCountDistinct")}</MenuItem>
            </Select>
          </FormControl>
          <TextField
            fullWidth
            size="small"
            label={t("kpis.wizard.v2.innerGrain")}
            value={form.inner_grain}
            onChange={(e) => onChange({ inner_grain: e.target.value })}
            placeholder={t("kpis.wizard.v2.innerGrainPlaceholder")}
          />
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.outerAgg")}</InputLabel>
            <Select
              value={form.outer_agg}
              label={t("kpis.wizard.v2.outerAgg")}
              onChange={(e) => onChange({ outer_agg: e.target.value })}
            >
              <MenuItem value="sum">{t("kpis.wizard.v2.aggSum")}</MenuItem>
              <MenuItem value="avg">{t("kpis.wizard.v2.aggAvg")}</MenuItem>
              <MenuItem value="min">{t("kpis.wizard.v2.aggMin")}</MenuItem>
              <MenuItem value="max">{t("kpis.wizard.v2.aggMax")}</MenuItem>
              <MenuItem value="count">{t("kpis.wizard.v2.aggCount")}</MenuItem>
              <MenuItem value="count_distinct">{t("kpis.wizard.v2.aggCountDistinct")}</MenuItem>
            </Select>
          </FormControl>
        </>
      )}

      {/* Hierarchy / Weight */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.parentKpi")}</InputLabel>
        <Select
          value={form.parent_kpi_id}
          label={t("kpis.parentKpi")}
          onChange={(e) => onChange({ parent_kpi_id: e.target.value })}
        >
          <MenuItem value="">{t("kpis.none")}</MenuItem>
          {kpis.map((k) => (
            <MenuItem key={k.id} value={k.id}>
              {k.display_name || k.name}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      <TextField
        size="small"
        fullWidth
        label={t("kpis.weight")}
        type="number"
        value={form.weight}
        onChange={(e) => onChange({ weight: e.target.value })}
        helperText={t("kpis.weightHelp")}
        inputProps={{ min: 0, max: 100, step: 0.1 }}
      />

      {/* Snapshot schedule — only shown for non-draft KPIs */}
      {form.certification_status !== "draft" && (
        <>
          <Divider sx={{ mt: 1 }} />
          <SnapshotSchedulePicker form={form} onChange={onChange} />
        </>
      )}
    </Box>
  );
}
