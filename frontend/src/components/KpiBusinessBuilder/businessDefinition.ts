import type {
  BusinessDefinition,
  BusinessFilter,
  BusinessFormula,
  BusinessFormulaType,
  BusinessTarget,
  BusinessTimeCalculation,
  BusinessTimeWindow,
  Direction,
  KpiPresentationMeta,
  PeriodGrain,
  PresentationType,
  TimeCalculationType,
  TimeWindowPreset,
} from "../../api/types_domains/kpis";

export type FormulaCategory = "measure" | "compare" | "analyze";

export interface FormulaFamilyEntry {
  type: BusinessFormulaType;
  labelKey: string;
  descKey: string;
  category: FormulaCategory;
}

export const FORMULA_CATEGORIES: {
  key: FormulaCategory;
  labelKey: string;
}[] = [
  { key: "measure", labelKey: "kpiBusiness.formulaCategoryMeasure" },
  { key: "compare", labelKey: "kpiBusiness.formulaCategoryCompare" },
  { key: "analyze", labelKey: "kpiBusiness.formulaCategoryAnalyze" },
];

export const FORMULA_FAMILIES: FormulaFamilyEntry[] = [
  { type: "single_measure", labelKey: "kpiBusiness.formulaSingleMeasure", descKey: "kpiBusiness.formulaSingleMeasureDesc", category: "measure" },
  { type: "count_records", labelKey: "kpiBusiness.formulaCountRecords", descKey: "kpiBusiness.formulaCountRecordsDesc", category: "measure" },
  { type: "count_distinct", labelKey: "kpiBusiness.formulaCountDistinct", descKey: "kpiBusiness.formulaCountDistinctDesc", category: "measure" },
  { type: "ratio", labelKey: "kpiBusiness.formulaRatio", descKey: "kpiBusiness.formulaRatioDesc", category: "compare" },
  { type: "compare_periods", labelKey: "kpiBusiness.formulaComparePeriods", descKey: "kpiBusiness.formulaComparePeriodsDesc", category: "compare" },
  { type: "compare_measures", labelKey: "kpiBusiness.formulaCompareMeasures", descKey: "kpiBusiness.formulaCompareMeasuresDesc", category: "compare" },
  { type: "target_comparison", labelKey: "kpiBusiness.formulaTargetComparison", descKey: "kpiBusiness.formulaTargetComparisonDesc", category: "compare" },
  { type: "moving_average", labelKey: "kpiBusiness.formulaMovingAverage", descKey: "kpiBusiness.formulaMovingAverageDesc", category: "analyze" },
  { type: "exception_sla", labelKey: "kpiBusiness.formulaExceptionSla", descKey: "kpiBusiness.formulaExceptionSlaDesc", category: "analyze" },
  { type: "share_rank", labelKey: "kpiBusiness.formulaShareRank", descKey: "kpiBusiness.formulaShareRankDesc", category: "analyze" },
  { type: "composite_score", labelKey: "kpiBusiness.formulaCompositeScore", descKey: "kpiBusiness.formulaCompositeScoreDesc", category: "analyze" },
];

export const TIME_CALC_OPTIONS: { type: TimeCalculationType; labelKey: string }[] = [
  { type: "current", labelKey: "kpiBusiness.timeCalcCurrent" },
  { type: "prior_period", labelKey: "kpiBusiness.timeCalcPriorPeriod" },
  { type: "period_to_date", labelKey: "kpiBusiness.timeCalcPeriodToDate" },
  { type: "trailing_sum", labelKey: "kpiBusiness.timeCalcTrailingSum" },
  { type: "moving_average", labelKey: "kpiBusiness.timeCalcMovingAverage" },
  { type: "lag", labelKey: "kpiBusiness.timeCalcLag" },
  { type: "lead", labelKey: "kpiBusiness.timeCalcLead" },
  { type: "percentage_change", labelKey: "kpiBusiness.timeCalcPercentageChange" },
  { type: "yoy_value", labelKey: "kpiBusiness.timeCalcYoyValue" },
  { type: "yoy_growth_pct", labelKey: "kpiBusiness.timeCalcYoyGrowthPct" },
  { type: "cagr", labelKey: "kpiBusiness.timeCalcCagr" },
];

export const TIME_WINDOW_PRESETS: { preset: TimeWindowPreset; labelKey: string }[] = [
  { preset: "today", labelKey: "kpiBusiness.twToday" },
  { preset: "this_week", labelKey: "kpiBusiness.twThisWeek" },
  { preset: "last_week", labelKey: "kpiBusiness.twLastWeek" },
  { preset: "last_complete_week", labelKey: "kpiBusiness.twLastCompleteWeek" },
  { preset: "this_month", labelKey: "kpiBusiness.twThisMonth" },
  { preset: "last_month", labelKey: "kpiBusiness.twLastMonth" },
  { preset: "last_complete_month", labelKey: "kpiBusiness.twLastCompleteMonth" },
  { preset: "this_quarter", labelKey: "kpiBusiness.twThisQuarter" },
  { preset: "last_quarter", labelKey: "kpiBusiness.twLastQuarter" },
  { preset: "last_complete_quarter", labelKey: "kpiBusiness.twLastCompleteQuarter" },
  { preset: "this_year", labelKey: "kpiBusiness.twThisYear" },
  { preset: "last_year", labelKey: "kpiBusiness.twLastYear" },
  { preset: "last_complete_year", labelKey: "kpiBusiness.twLastCompleteYear" },
  { preset: "last_7_days", labelKey: "kpiBusiness.twLast7Days" },
  { preset: "last_14_days", labelKey: "kpiBusiness.twLast14Days" },
  { preset: "last_30_days", labelKey: "kpiBusiness.twLast30Days" },
  { preset: "last_90_days", labelKey: "kpiBusiness.twLast90Days" },
  { preset: "last_3_months", labelKey: "kpiBusiness.twLast3Months" },
  { preset: "last_6_months", labelKey: "kpiBusiness.twLast6Months" },
  { preset: "last_12_months", labelKey: "kpiBusiness.twLast12Months" },
  { preset: "custom_range", labelKey: "kpiBusiness.twCustomRange" },
];

export const PERIOD_GRAINS: { grain: PeriodGrain; labelKey: string }[] = [
  { grain: "day", labelKey: "kpiBusiness.grainDay" },
  { grain: "week", labelKey: "kpiBusiness.grainWeek" },
  { grain: "month", labelKey: "kpiBusiness.grainMonth" },
  { grain: "quarter", labelKey: "kpiBusiness.grainQuarter" },
  { grain: "year", labelKey: "kpiBusiness.grainYear" },
];

export const COMPARISON_TYPES = [
  { value: "prior_period", labelKey: "kpiBusiness.compPriorPeriod" },
  { value: "same_period_last_year", labelKey: "kpiBusiness.compSamePeriodLastYear" },
  { value: "yoy", labelKey: "kpiBusiness.compYoy" },
  { value: "yoy_growth_pct", labelKey: "kpiBusiness.compYoyGrowthPct" },
  { value: "mom", labelKey: "kpiBusiness.compMom" },
  { value: "qoq", labelKey: "kpiBusiness.compQoq" },
];

export const AGGREGATION_OPTIONS = [
  { value: "sum", labelKey: "kpiBusiness.aggSum" },
  { value: "avg", labelKey: "kpiBusiness.aggAvg" },
  { value: "min", labelKey: "kpiBusiness.aggMin" },
  { value: "max", labelKey: "kpiBusiness.aggMax" },
  { value: "count", labelKey: "kpiBusiness.aggCount" },
  { value: "count_distinct", labelKey: "kpiBusiness.aggCountDistinct" },
];

export interface BusinessBuilderForm {
  name: string;
  formula: BusinessFormula;
  timeWindow: BusinessTimeWindow;
  filters: BusinessFilter[];
  target: BusinessTarget | null;
  direction: Direction;
  displayFolder: string;
  description: string;
  formatToken: string;
  unitLabel: string;
  trendPeriod: string;
  presentationType: PresentationType | "";
  presentationMeta: KpiPresentationMeta | null;
  bandsCustomized: boolean;
}

export function createDefaultForm(): BusinessBuilderForm {
  return {
    name: "",
    formula: { type: "single_measure" },
    timeWindow: {},
    filters: [],
    target: null,
    direction: "higher_is_better",
    displayFolder: "",
    description: "",
    formatToken: "",
    unitLabel: "",
    trendPeriod: "month",
    presentationType: "",
    presentationMeta: null,
    bandsCustomized: false,
  };
}

export function formToDefinition(form: BusinessBuilderForm): BusinessDefinition {
  const defn: BusinessDefinition = {
    builder: "business_kpi",
    version: 1,
    formula: form.formula,
    direction: form.direction,
  };

  if (form.timeWindow.dimension_id || form.timeWindow.preset) {
    defn.time_window = form.timeWindow;
  }

  if (form.filters.length > 0) {
    defn.filters = form.filters;
  }

  if (form.target) {
    defn.target = form.target;
  }

  return defn;
}

export function definitionToForm(bd: BusinessDefinition): BusinessBuilderForm {
  return {
    name: "",
    formula: bd.formula,
    timeWindow: bd.time_window || {},
    filters: bd.filters || [],
    target: bd.target || null,
    direction: bd.direction || "higher_is_better",
    displayFolder: "",
    description: "",
    formatToken: "",
    unitLabel: "",
    trendPeriod: "month",
    presentationType: "",
    presentationMeta: null,
    bandsCustomized: false,
  };
}
