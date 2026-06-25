/**
 * KPI Wizard Step 1 — KPI type selection and measure inputs.
 *
 * The user picks one of the 6 canonical KPI types, then fills in
 * the type-specific measure inputs (value, goal/denominator, etc.).
 *
 * When a template expression is active, a measure-mapping section is
 * shown instead so the user can bind template placeholders to real
 * model measures.
 */
import { useCallback, useMemo, useState } from "react";
import {
  Alert,
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
import { KPI_TYPE_OPTIONS } from "./types";
import {
  extractMeasureReferences,
  rewriteMeasureReferences,
} from "./expressionBuilder";

interface Props {
  form: KpiWizardFormState;
  onChange: (patch: Partial<KpiWizardFormState>) => void;
  measures: Measure[];
  generatedExpression: string;
  /** The raw template expression before any mapping, if a template was applied. */
  templateExpression?: string;
}

export default function KpiWizardStep1TypeInputs({
  form,
  onChange,
  measures,
  generatedExpression,
  templateExpression,
}: Props) {
  const t = useT();

  // Template measure references (stable across re-renders).
  const templateRefs = useMemo(
    () => (templateExpression ? extractMeasureReferences(templateExpression) : []),
    [templateExpression],
  );

  const isTemplateMode = templateRefs.length > 0;

  // Mapping state: template measure name → selected measure id
  const [mapping, setMapping] = useState<Record<string, string>>({});

  const handleMappingChange = useCallback(
    (templateName: string, measureId: string) => {
      const next = { ...mapping, [templateName]: measureId };
      setMapping(next);

      // Build name-to-name mapping for expression rewrite
      const nameMapping: Record<string, string> = {};
      for (const [tplName, mId] of Object.entries(next)) {
        const m = measures.find((x) => x.id === mId);
        if (m) nameMapping[tplName] = m.name;
      }

      // Rewrite expression
      const rewritten = rewriteMeasureReferences(templateExpression!, nameMapping);
      onChange({ expression: rewritten });
    },
    [mapping, measures, templateExpression, onChange],
  );

  const allMapped = isTemplateMode && templateRefs.every((n) => mapping[n]);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
      <Typography variant="h6">{t("kpis.wizard.v2.typeTitle")}</Typography>
      <Typography variant="body2" color="text.secondary">
        {t("kpis.wizard.v2.typeSubtitle")}
      </Typography>

      {/* KPI type selector */}
      <FormControl fullWidth size="small">
        <InputLabel>{t("kpis.wizard.v2.kpiTypeLabel")}</InputLabel>
        <Select
          value={form.kpi_type}
          label={t("kpis.wizard.v2.kpiTypeLabel")}
          onChange={(e) => onChange({ kpi_type: e.target.value as KpiWizardFormState["kpi_type"] })}
        >
          {KPI_TYPE_OPTIONS.map((opt) => (
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

      {/* Template measure mapping (shown when template is active) */}
      {isTemplateMode && (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          <Typography variant="subtitle2">
            {t("kpis.wizard.v2.mapMeasures")}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {t("kpis.wizard.v2.mapMeasuresHelp")}
          </Typography>
          {templateRefs.map((refName) => (
            <Box key={refName} sx={{ display: "flex", gap: 1.5, alignItems: "center" }}>
              <TextField
                size="small"
                label={t("kpis.wizard.v2.templateMeasure")}
                value={refName}
                InputProps={{ readOnly: true }}
                sx={{ flex: 1, "& .MuiInputBase-input": { fontFamily: "monospace", fontSize: 13 } }}
              />
              <FormControl size="small" sx={{ flex: 1 }}>
                <InputLabel>{t("kpis.wizard.v2.yourMeasure")}</InputLabel>
                <Select
                  value={mapping[refName] ?? ""}
                  label={t("kpis.wizard.v2.yourMeasure")}
                  onChange={(e) => handleMappingChange(refName, e.target.value)}
                >
                  <MenuItem value="">{t("kpis.none")}</MenuItem>
                  {measures.map((m) => (
                    <MenuItem key={m.id} value={m.id}>
                      {m.display_name || m.name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Box>
          ))}
          {!allMapped && (
            <Alert severity="info" sx={{ mt: 0.5 }}>
              {t("kpis.wizard.v2.mapMeasuresIncomplete")}
            </Alert>
          )}
        </Box>
      )}

      {/* Type-specific inputs (hidden in template mode) */}
      {!isTemplateMode && (form.kpi_type === "simple_measure" || form.kpi_type === "growth_rate" || form.kpi_type === "moving_window") && (
        <FormControl fullWidth size="small">
          <InputLabel>{t("kpis.wizard.v2.valueMeasure")}</InputLabel>
          <Select
            value={form.primaryMeasure}
            label={t("kpis.wizard.v2.valueMeasure")}
            onChange={(e) => onChange({ primaryMeasure: e.target.value })}
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

      {!isTemplateMode && form.kpi_type === "ratio" && (
        <>
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.numerator")}</InputLabel>
            <Select
              value={form.primaryMeasure}
              label={t("kpis.wizard.v2.numerator")}
              onChange={(e) => onChange({ primaryMeasure: e.target.value })}
            >
              <MenuItem value="">{t("kpis.none")}</MenuItem>
              {measures.map((m) => (
                <MenuItem key={m.id} value={m.id}>
                  {m.display_name || m.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.denominator")}</InputLabel>
            <Select
              value={form.secondaryMeasure}
              label={t("kpis.wizard.v2.denominator")}
              onChange={(e) => onChange({ secondaryMeasure: e.target.value })}
            >
              <MenuItem value="">{t("kpis.none")}</MenuItem>
              {measures.map((m) => (
                <MenuItem key={m.id} value={m.id}>
                  {m.display_name || m.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </>
      )}

      {!isTemplateMode && form.kpi_type === "variance" && (
        <>
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.valueMeasure")}</InputLabel>
            <Select
              value={form.primaryMeasure}
              label={t("kpis.wizard.v2.valueMeasure")}
              onChange={(e) => onChange({ primaryMeasure: e.target.value })}
            >
              <MenuItem value="">{t("kpis.none")}</MenuItem>
              {measures.map((m) => (
                <MenuItem key={m.id} value={m.id}>
                  {m.display_name || m.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth size="small">
            <InputLabel>{t("kpis.wizard.v2.comparisonMeasure")}</InputLabel>
            <Select
              value={form.secondaryMeasure}
              label={t("kpis.wizard.v2.comparisonMeasure")}
              onChange={(e) => onChange({ secondaryMeasure: e.target.value })}
            >
              <MenuItem value="">{t("kpis.none")}</MenuItem>
              {measures.map((m) => (
                <MenuItem key={m.id} value={m.id}>
                  {m.display_name || m.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </>
      )}

      {!isTemplateMode && (form.kpi_type === "growth_rate" || form.kpi_type === "moving_window") && (
        <FormControl fullWidth size="small">
          <InputLabel>{t("kpis.wizard.v2.periodGrain")}</InputLabel>
          <Select
            value={form.trend_period}
            label={t("kpis.wizard.v2.periodGrain")}
            onChange={(e) => onChange({ trend_period: e.target.value })}
          >
            <MenuItem value="day">{t("kpis.wizard.v2.periodDay")}</MenuItem>
            <MenuItem value="week">{t("kpis.wizard.v2.periodWeek")}</MenuItem>
            <MenuItem value="month">{t("kpis.wizard.v2.periodMonth")}</MenuItem>
            <MenuItem value="quarter">{t("kpis.wizard.v2.periodQuarter")}</MenuItem>
            <MenuItem value="year">{t("kpis.wizard.v2.periodYear")}</MenuItem>
          </Select>
        </FormControl>
      )}

      {!isTemplateMode && form.kpi_type === "moving_window" && (
        <TextField
          size="small"
          fullWidth
          label={t("kpis.wizard.v2.windowSize")}
          type="number"
          value={form.trend_sparkline_periods}
          onChange={(e) => onChange({ trend_sparkline_periods: e.target.value })}
          inputProps={{ min: 2, max: 52 }}
        />
      )}

      {/* Generated expression preview */}
      {generatedExpression && (
        <Box sx={{ mt: 1 }}>
          <Typography variant="caption" color="text.secondary">
            {t("kpis.wizard.v2.expressionLabel")}
          </Typography>
          <TextField
            size="small"
            fullWidth
            multiline
            minRows={1}
            maxRows={3}
            value={generatedExpression}
            onChange={(e) => onChange({ expression: e.target.value })}
            sx={{ fontFamily: "monospace", fontSize: 13, mt: 0.5 }}
          />
          <FormHelperText>{t("kpis.wizard.v2.expressionHelp")}</FormHelperText>
        </Box>
      )}
    </Box>
  );
}
