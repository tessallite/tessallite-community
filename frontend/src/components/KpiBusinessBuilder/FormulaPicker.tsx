import { useMemo, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  ButtonBase,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";

import { useT } from "../../i18n";
import type { Dimension, Measure } from "../../api/types";
import type {
  BusinessFormula,
  BusinessTimeCalculation,
  PeriodGrain,
  TimeCalculationType,
} from "../../api/types_domains/kpis";
import {
  AGGREGATION_OPTIONS,
  COMPARISON_TYPES,
  type FormulaCategory,
  FORMULA_CATEGORIES,
  FORMULA_FAMILIES,
  PERIOD_GRAINS,
  TIME_CALC_OPTIONS,
} from "./businessDefinition";

type Props = {
  formula: BusinessFormula;
  onChange: (formula: BusinessFormula) => void;
  measures: Measure[];
  dimensions: Dimension[];
};

function MeasurePicker({
  label,
  value,
  onChange,
  measures,
}: {
  label: string;
  value: string | undefined;
  onChange: (id: string | undefined) => void;
  measures: Measure[];
}) {
  const selected = measures.find((m) => m.id === value) ?? null;
  return (
    <Autocomplete
      size="small"
      options={measures}
      getOptionLabel={(m) => m.display_name || m.name}
      value={selected}
      onChange={(_, v) => onChange(v?.id)}
      renderInput={(params) => <TextField {...params} label={label} />}
      isOptionEqualToValue={(a, b) => a.id === b.id}
    />
  );
}

function DimensionPicker({
  label,
  value,
  onChange,
  dimensions,
}: {
  label: string;
  value: string | undefined;
  onChange: (id: string | undefined) => void;
  dimensions: Dimension[];
}) {
  const selected = dimensions.find((d) => d.id === value) ?? null;
  return (
    <Autocomplete
      size="small"
      options={dimensions}
      getOptionLabel={(d) => d.display_name || d.name}
      value={selected}
      onChange={(_, v) => onChange(v?.id)}
      renderInput={(params) => <TextField {...params} label={label} />}
      isOptionEqualToValue={(a, b) => a.id === b.id}
    />
  );
}

function TimeCalculationSection({
  timeCalc,
  onChange,
  label,
}: {
  timeCalc: BusinessTimeCalculation | undefined;
  onChange: (tc: BusinessTimeCalculation | undefined) => void;
  label?: string;
}) {
  const t = useT();

  if (!timeCalc) {
    return (
      <Box>
        <Typography
          variant="caption"
          color="primary"
          sx={{ cursor: "pointer" }}
          onClick={() => onChange({ type: "current" })}
        >
          + {label ?? t("kpiBusiness.addTimeCalc")}
        </Typography>
      </Box>
    );
  }

  const needsPeriods = ["trailing_sum", "moving_average", "lag", "lead"].includes(
    timeCalc.type,
  );

  return (
    <Stack spacing={1.5}>
      <Stack direction="row" spacing={1} alignItems="center">
        <FormControl size="small" sx={{ flex: 1 }}>
          <InputLabel>{t("kpiBusiness.timeCalcType")}</InputLabel>
          <Select
            label={t("kpiBusiness.timeCalcType")}
            value={timeCalc.type}
            onChange={(e) =>
              onChange({ ...timeCalc, type: e.target.value as TimeCalculationType })
            }
          >
            {TIME_CALC_OPTIONS.map((o) => (
              <MenuItem key={o.type} value={o.type}>
                {t(o.labelKey)}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <IconButton size="small" onClick={() => onChange(undefined)}>
          <DeleteIcon fontSize="small" />
        </IconButton>
      </Stack>

      {needsPeriods && (
        <Stack direction="row" spacing={1}>
          <TextField
            size="small"
            type="number"
            label={t("kpiBusiness.periods")}
            value={timeCalc.periods ?? ""}
            onChange={(e) =>
              onChange({
                ...timeCalc,
                periods: e.target.value ? Number(e.target.value) : undefined,
              })
            }
            sx={{ flex: 1 }}
            inputProps={{ min: 1 }}
          />
          <FormControl size="small" sx={{ flex: 1 }}>
            <InputLabel>{t("kpiBusiness.grain")}</InputLabel>
            <Select
              label={t("kpiBusiness.grain")}
              value={timeCalc.grain ?? ""}
              onChange={(e) =>
                onChange({
                  ...timeCalc,
                  grain: (e.target.value as PeriodGrain) || undefined,
                })
              }
            >
              {PERIOD_GRAINS.map((g) => (
                <MenuItem key={g.grain} value={g.grain}>
                  {t(g.labelKey)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </Stack>
      )}
    </Stack>
  );
}

function isNaiveAverageRisk(measure: Measure | undefined): boolean {
  if (!measure) return false;
  if (measure.format === "percent" || measure.format === "percent_2dp") return true;
  if (measure.is_additive === false) return true;
  return false;
}

export function FormulaPicker({ formula, onChange, measures, dimensions }: Props) {
  const t = useT();

  const selectedFamily = useMemo(
    () => FORMULA_FAMILIES.find((f) => f.type === formula.type),
    [formula.type],
  );

  const activeCategory: FormulaCategory = selectedFamily?.category ?? "measure";
  const [visibleCategory, setVisibleCategory] = useState<FormulaCategory>(activeCategory);

  const categoryFamilies = useMemo(
    () => FORMULA_FAMILIES.filter((f) => f.category === visibleCategory),
    [visibleCategory],
  );

  function patch(p: Partial<BusinessFormula>) {
    onChange({ ...formula, ...p });
  }

  function setType(type: BusinessFormula["type"]) {
    const base: BusinessFormula = { type };
    if (type === "exception_sla") {
      base.sla_type = "compliance_pct";
      base.comparator = ">=";
    }
    onChange(base);
  }

  return (
    <Box>
      <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>
        {t("kpiBusiness.formulaSection")}
      </Typography>

      <Stack spacing={2}>
        <ToggleButtonGroup
          value={visibleCategory}
          exclusive
          onChange={(_, v) => { if (v) setVisibleCategory(v); }}
          size="small"
          fullWidth
          sx={{
            "& .MuiToggleButton-root": {
              textTransform: "none",
              fontWeight: 600,
              fontSize: 13,
              py: 0.75,
            },
          }}
        >
          {FORMULA_CATEGORIES.map((cat) => (
            <ToggleButton key={cat.key} value={cat.key}>
              {t(cat.labelKey)}
            </ToggleButton>
          ))}
        </ToggleButtonGroup>

        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
            gap: 1,
          }}
        >
          {categoryFamilies.map((f) => {
            const isSelected = formula.type === f.type;
            return (
              <ButtonBase
                key={f.type}
                onClick={() => {
                  setType(f.type);
                  setVisibleCategory(f.category);
                }}
                sx={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-start",
                  textAlign: "left",
                  p: 1.25,
                  borderRadius: 1,
                  border: isSelected
                    ? "2px solid"
                    : "1px solid",
                  borderColor: isSelected
                    ? "primary.main"
                    : "divider",
                  bgcolor: isSelected
                    ? "action.selected"
                    : "background.paper",
                  transition: "border-color 0.15s, background-color 0.15s",
                  "&:hover": {
                    borderColor: isSelected ? "primary.main" : "text.secondary",
                    bgcolor: isSelected ? "action.selected" : "action.hover",
                  },
                }}
              >
                <Typography
                  variant="body2"
                  fontWeight={isSelected ? 700 : 600}
                  sx={{
                    color: isSelected ? "primary.main" : "text.primary",
                    lineHeight: 1.3,
                  }}
                >
                  {t(f.labelKey)}
                </Typography>
                <Typography
                  variant="caption"
                  sx={{
                    color: "text.secondary",
                    lineHeight: 1.3,
                    mt: 0.25,
                    fontSize: 11,
                  }}
                >
                  {t(f.descKey)}
                </Typography>
              </ButtonBase>
            );
          })}
        </Box>

        {/* --- single_measure --- */}
        {formula.type === "single_measure" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <FormControl size="small">
              <InputLabel>{t("kpiBusiness.aggregation")}</InputLabel>
              <Select
                label={t("kpiBusiness.aggregation")}
                value={formula.aggregation ?? "sum"}
                onChange={(e) => patch({ aggregation: e.target.value })}
              >
                {AGGREGATION_OPTIONS.map((a) => (
                  <MenuItem key={a.value} value={a.value}>
                    {t(a.labelKey)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <TimeCalculationSection
              timeCalc={formula.time_calculation}
              onChange={(tc) => patch({ time_calculation: tc })}
            />
            {formula.time_calculation?.type === "moving_average" &&
              isNaiveAverageRisk(measures.find((m) => m.id === formula.measure_id)) && (
              <Alert severity="warning" variant="outlined" sx={{ py: 0.5, "& .MuiAlert-message": { fontSize: 12 } }}>
                {t("kpiBusiness.naiveAverageWarning")}
              </Alert>
            )}
          </>
        )}

        {/* --- ratio --- */}
        {formula.type === "ratio" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.numerator")}
              value={formula.numerator_measure_id}
              onChange={(id) => patch({ numerator_measure_id: id })}
              measures={measures}
            />
            <MeasurePicker
              label={t("kpiBusiness.denominator")}
              value={formula.denominator_measure_id}
              onChange={(id) => patch({ denominator_measure_id: id })}
              measures={measures}
            />
          </>
        )}

        {/* --- count_records --- */}
        {formula.type === "count_records" && null}

        {/* --- count_distinct --- */}
        {formula.type === "count_distinct" && (
            <DimensionPicker
              label={t("kpiBusiness.distinctDimension")}
              value={formula.dimension_id}
              onChange={(id) => patch({ dimension_id: id })}
              dimensions={dimensions}
            />
        )}

        {/* --- moving_average --- */}
        {formula.type === "moving_average" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <Stack direction="row" spacing={1}>
              <TextField
                size="small"
                type="number"
                label={t("kpiBusiness.windowSize")}
                value={formula.window_size ?? ""}
                onChange={(e) =>
                  patch({ window_size: e.target.value ? Number(e.target.value) : undefined })
                }
                sx={{ flex: 1 }}
                inputProps={{ min: 1 }}
              />
              <FormControl size="small" sx={{ flex: 1 }}>
                <InputLabel>{t("kpiBusiness.grain")}</InputLabel>
                <Select
                  label={t("kpiBusiness.grain")}
                  value={formula.grain ?? "month"}
                  onChange={(e) => patch({ grain: e.target.value as PeriodGrain })}
                >
                  {PERIOD_GRAINS.map((g) => (
                    <MenuItem key={g.grain} value={g.grain}>
                      {t(g.labelKey)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Stack>
            {isNaiveAverageRisk(measures.find((m) => m.id === formula.measure_id)) && (
              <Alert severity="warning" variant="outlined" sx={{ py: 0.5, "& .MuiAlert-message": { fontSize: 12 } }}>
                {t("kpiBusiness.naiveAverageWarning")}
              </Alert>
            )}
          </>
        )}

        {/* --- compare_periods --- */}
        {formula.type === "compare_periods" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <FormControl size="small">
              <InputLabel>{t("kpiBusiness.comparisonType")}</InputLabel>
              <Select
                label={t("kpiBusiness.comparisonType")}
                value={formula.comparison ?? ""}
                onChange={(e) => patch({ comparison: e.target.value })}
              >
                {COMPARISON_TYPES.map((c) => (
                  <MenuItem key={c.value} value={c.value}>
                    {t(c.labelKey)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </>
        )}

        {/* --- compare_measures --- */}
        {formula.type === "compare_measures" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measureA")}
              value={formula.measure_a_id}
              onChange={(id) => patch({ measure_a_id: id })}
              measures={measures}
            />
            <MeasurePicker
              label={t("kpiBusiness.measureB")}
              value={formula.measure_b_id}
              onChange={(id) => patch({ measure_b_id: id })}
              measures={measures}
            />
            <FormControl size="small">
              <InputLabel>{t("kpiBusiness.comparisonMode")}</InputLabel>
              <Select
                label={t("kpiBusiness.comparisonMode")}
                value={formula.mode ?? "absolute"}
                onChange={(e) => patch({ mode: e.target.value })}
              >
                <MenuItem value="absolute">{t("kpiBusiness.modeDifference")}</MenuItem>
                <MenuItem value="percentage">{t("kpiBusiness.modePercentage")}</MenuItem>
              </Select>
            </FormControl>
          </>
        )}

        {/* --- target_comparison --- */}
        {formula.type === "target_comparison" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <MeasurePicker
              label={t("kpiBusiness.targetMeasure")}
              value={formula.denominator_measure_id}
              onChange={(id) => patch({ denominator_measure_id: id })}
              measures={measures}
            />
            <FormControl size="small">
              <InputLabel>{t("kpiBusiness.comparisonMode")}</InputLabel>
              <Select
                label={t("kpiBusiness.comparisonMode")}
                value={formula.mode ?? "variance_pct"}
                onChange={(e) => patch({ mode: e.target.value })}
              >
                <MenuItem value="variance_pct">{t("kpiBusiness.modeVariancePct")}</MenuItem>
                <MenuItem value="variance_abs">{t("kpiBusiness.modeVarianceAbs")}</MenuItem>
                <MenuItem value="attainment_pct">{t("kpiBusiness.modeAttainmentPct")}</MenuItem>
              </Select>
            </FormControl>
          </>
        )}

        {/* --- exception_sla --- */}
        {formula.type === "exception_sla" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <FormControl size="small">
              <InputLabel>{t("kpiBusiness.slaType")}</InputLabel>
              <Select
                label={t("kpiBusiness.slaType")}
                value={formula.sla_type ?? "compliance_pct"}
                onChange={(e) => patch({ sla_type: e.target.value })}
              >
                <MenuItem value="compliance_pct">{t("kpiBusiness.slaCompliancePct")}</MenuItem>
                <MenuItem value="exception_count">{t("kpiBusiness.slaBreachCount")}</MenuItem>
                <MenuItem value="backlog">{t("kpiBusiness.slaBacklog")}</MenuItem>
              </Select>
            </FormControl>
            {(formula.sla_type === "compliance_pct" || formula.sla_type === "exception_count" || !formula.sla_type) && (
              <Stack direction="row" spacing={1}>
                <FormControl size="small" sx={{ flex: 1 }}>
                  <InputLabel>{t("kpiBusiness.slaComparator")}</InputLabel>
                  <Select
                    label={t("kpiBusiness.slaComparator")}
                    value={formula.comparator ?? ">="}
                    onChange={(e) => patch({ comparator: e.target.value })}
                  >
                    <MenuItem value=">">{">"}</MenuItem>
                    <MenuItem value=">=">{">="}</MenuItem>
                    <MenuItem value="<">{"<"}</MenuItem>
                    <MenuItem value="<=">{"<="}</MenuItem>
                    <MenuItem value="=">{"="}</MenuItem>
                    <MenuItem value="!=">{"!="}</MenuItem>
                  </Select>
                </FormControl>
                <TextField
                  size="small"
                  type="number"
                  label={t("kpiBusiness.slaThreshold")}
                  value={formula.threshold_value ?? ""}
                  onChange={(e) =>
                    patch({ threshold_value: e.target.value ? Number(e.target.value) : undefined })
                  }
                  sx={{ flex: 1 }}
                />
              </Stack>
            )}
            <DimensionPicker
              label={t("kpiBusiness.thresholdDimension")}
              value={formula.dimension_id}
              onChange={(id) => patch({ dimension_id: id })}
              dimensions={dimensions}
            />
          </>
        )}

        {/* --- share_rank --- */}
        {formula.type === "share_rank" && (
          <>
            <MeasurePicker
              label={t("kpiBusiness.measure")}
              value={formula.measure_id}
              onChange={(id) => patch({ measure_id: id })}
              measures={measures}
            />
            <DimensionPicker
              label={t("kpiBusiness.byDimension")}
              value={formula.by_dimension_id}
              onChange={(id) => patch({ by_dimension_id: id })}
              dimensions={dimensions}
            />
            <Stack direction="row" spacing={1}>
              <FormControl size="small" sx={{ flex: 1 }}>
                <InputLabel>{t("kpiBusiness.shareType")}</InputLabel>
                <Select
                  label={t("kpiBusiness.shareType")}
                  value={formula.share_type ?? "share_of_total"}
                  onChange={(e) => patch({ share_type: e.target.value })}
                >
                  <MenuItem value="share_of_total">{t("kpiBusiness.sharePercent")}</MenuItem>
                  <MenuItem value="rank">{t("kpiBusiness.rank")}</MenuItem>
                  <MenuItem value="top_n_contribution">{t("kpiBusiness.topN")}</MenuItem>
                </Select>
              </FormControl>
              {formula.share_type === "top_n_contribution" && (
                <TextField
                  size="small"
                  type="number"
                  label={t("kpiBusiness.nValue")}
                  value={formula.n ?? ""}
                  onChange={(e) =>
                    patch({ n: e.target.value ? Number(e.target.value) : undefined })
                  }
                  sx={{ width: 80 }}
                  inputProps={{ min: 1 }}
                />
              )}
            </Stack>
            {(formula.share_type === "rank" || formula.share_type === "share_of_total" || !formula.share_type) && (
              <Alert severity="info" variant="outlined" sx={{ py: 0.5, "& .MuiAlert-message": { fontSize: 12 } }}>
                {formula.share_type === "rank"
                  ? t("kpiBusiness.rankNoMemberHint")
                  : t("kpiBusiness.shareNoMemberHint")}
              </Alert>
            )}
          </>
        )}

        {/* --- composite_score --- */}
        {formula.type === "composite_score" && (
          <CompositeScoreEditor
            components={formula.components ?? []}
            onChange={(c) => patch({ components: c })}
            measures={measures}
          />
        )}
      </Stack>
    </Box>
  );
}

function CompositeScoreEditor({
  components,
  onChange,
  measures,
}: {
  components: NonNullable<BusinessFormula["components"]>;
  onChange: (c: NonNullable<BusinessFormula["components"]>) => void;
  measures: Measure[];
}) {
  const t = useT();

  function updateAt(idx: number, patch: Partial<(typeof components)[number]>) {
    const next = components.map((c, i) => (i === idx ? { ...c, ...patch } : c));
    onChange(next);
  }

  function addComponent() {
    onChange([...components, { weight: 1 }]);
  }

  function removeAt(idx: number) {
    onChange(components.filter((_, i) => i !== idx));
  }

  return (
    <Stack spacing={1.5}>
      <Typography variant="caption" color="text.secondary">
        {t("kpiBusiness.compositeComponents")}
      </Typography>
      {components.map((comp, i) => (
        <Stack key={i} direction="row" spacing={1} alignItems="center">
          <MeasurePicker
            label={`${t("kpiBusiness.measure")} ${i + 1}`}
            value={comp.measure_id}
            onChange={(id) => updateAt(i, { measure_id: id })}
            measures={measures}
          />
          <TextField
            size="small"
            type="number"
            label={t("kpiBusiness.weight")}
            value={comp.weight ?? 1}
            onChange={(e) =>
              updateAt(i, { weight: e.target.value ? Number(e.target.value) : 1 })
            }
            sx={{ width: 80 }}
            inputProps={{ min: 0, step: 0.1 }}
          />
          <IconButton size="small" onClick={() => removeAt(i)}>
            <DeleteIcon fontSize="small" />
          </IconButton>
        </Stack>
      ))}
      <Box>
        <IconButton size="small" onClick={addComponent} color="primary">
          <AddIcon fontSize="small" />
        </IconButton>
      </Box>
    </Stack>
  );
}
