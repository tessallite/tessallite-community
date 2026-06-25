/**
 * Shared types for the KPI v2 wizard components.
 */
import type {
  CalcAggMode,
  Direction,
  FormatToken,
  IndicatorType,
  KpiPresentationMeta,
  KpiType,
  PresentationType,
  TargetType,
} from "../../api/types";

/** Wizard step identifiers. */
export type WizardStep = 0 | 1 | 2 | 3 | 4;

/** Full wizard form state. */
export interface KpiWizardFormState {
  // Identity
  name: string;
  display_name: string;
  description: string;
  display_folder: string;

  // Type & expression
  kpi_type: KpiType | "";
  expression: string;

  // Aggregation
  calc_agg_mode: CalcAggMode;
  inner_agg: string;
  inner_grain: string;
  outer_agg: string;

  // Target
  target_type: TargetType | "";
  target_value: string;
  target_measure_id: string;
  target_expression: string;
  target_period: string;

  // Direction & thresholds
  direction: Direction;
  presentation_type: PresentationType | "";
  presentation_meta: KpiPresentationMeta | null;

  // Trend
  trend_period: string;
  trend_threshold: string;
  trend_sparkline_periods: string;

  // Formatting
  format_token: FormatToken | "";
  format_custom: string;
  unit_label: string;
  null_display_value: string;

  // Hierarchy / composition
  weight: string;
  parent_kpi_id: string;
  indicator_type: IndicatorType;

  // Time dimension
  time_dimension_id: string;

  // Snapshots
  snapshot_frequency: string;
  snapshot_retention: string;

  // Governance
  certification_status: string;
  owner_user_id: string;

  // Wizard-local measure selection (feeds into buildExpression, not sent to API)
  primaryMeasure: string;
  secondaryMeasure: string;
  status_graphic: string;
  trend_graphic: string;
}

/** Default empty form state. */
export const EMPTY_FORM: KpiWizardFormState = {
  name: "",
  display_name: "",
  description: "",
  display_folder: "",
  kpi_type: "",
  expression: "",
  calc_agg_mode: "automatic",
  inner_agg: "",
  inner_grain: "",
  outer_agg: "",
  target_type: "",
  target_value: "",
  target_measure_id: "",
  target_expression: "",
  target_period: "",
  direction: "higher_is_better",
  presentation_type: "",
  presentation_meta: null,
  trend_period: "month",
  trend_threshold: "0.01",
  trend_sparkline_periods: "12",
  format_token: "",
  format_custom: "",
  unit_label: "",
  null_display_value: "N/A",
  weight: "",
  parent_kpi_id: "",
  indicator_type: "none",
  time_dimension_id: "",
  snapshot_frequency: "",
  snapshot_retention: "90",
  certification_status: "draft",
  owner_user_id: "",
  primaryMeasure: "",
  secondaryMeasure: "",
  status_graphic: "Traffic Light",
  trend_graphic: "Standard Arrow",
};

/** KPI type descriptors for the type selector. */
export const KPI_TYPE_OPTIONS: {
  value: KpiType;
  labelKey: string;
  descKey: string;
}[] = [
  { value: "simple_measure", labelKey: "kpis.wizard.v2.typeSimpleMeasure", descKey: "kpis.wizard.v2.typeSimpleMeasureDesc" },
  { value: "ratio", labelKey: "kpis.wizard.v2.typeRatio", descKey: "kpis.wizard.v2.typeRatioDesc" },
  { value: "variance", labelKey: "kpis.wizard.v2.typeVariance", descKey: "kpis.wizard.v2.typeVarianceDesc" },
  { value: "growth_rate", labelKey: "kpis.wizard.v2.typeGrowthRate", descKey: "kpis.wizard.v2.typeGrowthRateDesc" },
  { value: "moving_window", labelKey: "kpis.wizard.v2.typeMovingWindow", descKey: "kpis.wizard.v2.typeMovingWindowDesc" },
  { value: "composite", labelKey: "kpis.wizard.v2.typeComposite", descKey: "kpis.wizard.v2.typeCompositeDesc" },
];

/** Direction options for the direction selector. */
export const DIRECTION_OPTIONS: {
  value: Direction;
  labelKey: string;
  descKey: string;
}[] = [
  { value: "higher_is_better", labelKey: "kpis.wizard.v2.directionHigher", descKey: "kpis.wizard.v2.directionHigherDesc" },
  { value: "lower_is_better", labelKey: "kpis.wizard.v2.directionLower", descKey: "kpis.wizard.v2.directionLowerDesc" },
  { value: "closer_is_better", labelKey: "kpis.wizard.v2.directionCloser", descKey: "kpis.wizard.v2.directionCloserDesc" },
];

/** Format token options for the format selector. */
export const FORMAT_TOKEN_OPTIONS: {
  value: FormatToken;
  labelKey: string;
}[] = [
  { value: "currency", labelKey: "kpis.wizard.v2.formatCurrency" },
  { value: "currency_k", labelKey: "kpis.wizard.v2.formatCurrencyK" },
  { value: "percent", labelKey: "kpis.wizard.v2.formatPercent" },
  { value: "percent_decimal", labelKey: "kpis.wizard.v2.formatPercentDecimal" },
  { value: "decimal_0dp", labelKey: "kpis.wizard.v2.formatDecimal0" },
  { value: "decimal_1dp", labelKey: "kpis.wizard.v2.formatDecimal1" },
  { value: "decimal_2dp", labelKey: "kpis.wizard.v2.formatDecimal2" },
  { value: "integer", labelKey: "kpis.wizard.v2.formatInteger" },
  { value: "custom", labelKey: "kpis.wizard.v2.formatCustom" },
];

/** Target type options. */
export const TARGET_TYPE_OPTIONS: {
  value: TargetType;
  labelKey: string;
}[] = [
  { value: "none", labelKey: "kpis.wizard.v2.targetTypeNone" },
  { value: "static", labelKey: "kpis.wizard.v2.targetTypeStatic" },
  { value: "measure", labelKey: "kpis.wizard.v2.targetTypeMeasure" },
  { value: "prior_period", labelKey: "kpis.wizard.v2.targetTypePriorPeriod" },
  { value: "expression", labelKey: "kpis.wizard.v2.targetTypeExpression" },
];

/** Presentation type options for the visual type selector. */
export const PRESENTATION_TYPE_OPTIONS: {
  value: PresentationType;
  labelKey: string;
  descKey: string;
}[] = [
  { value: "traffic_light", labelKey: "kpis.wizard.v2.visualTrafficLight", descKey: "kpis.wizard.v2.visualTrafficLightDesc" },
  { value: "gauge", labelKey: "kpis.wizard.v2.visualGauge", descKey: "kpis.wizard.v2.visualGaugeDesc" },
  { value: "bullet_chart", labelKey: "kpis.wizard.v2.visualBullet", descKey: "kpis.wizard.v2.visualBulletDesc" },
  { value: "rag_bar", labelKey: "kpis.wizard.v2.visualRagBar", descKey: "kpis.wizard.v2.visualRagBarDesc" },
  { value: "progress_ring", labelKey: "kpis.wizard.v2.visualProgressRing", descKey: "kpis.wizard.v2.visualProgressRingDesc" },
  { value: "thermometer", labelKey: "kpis.wizard.v2.visualThermometer", descKey: "kpis.wizard.v2.visualThermometerDesc" },
];

/** Snapshot frequency options. Maps user-facing labels to cron strings. */
export const SNAPSHOT_FREQUENCY_OPTIONS: {
  value: string;
  labelKey: string;
  cron: string;
}[] = [
  { value: "", labelKey: "kpis.wizard.v2.snapshot.freqNone", cron: "" },
  { value: "0 * * * *", labelKey: "kpis.wizard.v2.snapshot.freqHourly", cron: "0 * * * *" },
  { value: "0 0 * * *", labelKey: "kpis.wizard.v2.snapshot.freqDailyMidnight", cron: "0 0 * * *" },
  { value: "0 6 * * *", labelKey: "kpis.wizard.v2.snapshot.freqDaily6am", cron: "0 6 * * *" },
  { value: "0 0 * * 1", labelKey: "kpis.wizard.v2.snapshot.freqWeekly", cron: "0 0 * * 1" },
  { value: "0 0 1 * *", labelKey: "kpis.wizard.v2.snapshot.freqMonthly", cron: "0 0 1 * *" },
  { value: "0 0 1 1,4,7,10 *", labelKey: "kpis.wizard.v2.snapshot.freqQuarterly", cron: "0 0 1 1,4,7,10 *" },
];

/** Threshold preset options. */
export const THRESHOLD_PRESETS = [
  { value: "standard_3_band", labelKey: "kpis.wizard.v2.presetStandard3" },
  { value: "standard_4_band", labelKey: "kpis.wizard.v2.presetStandard4" },
  { value: "tight_tolerance", labelKey: "kpis.wizard.v2.presetTight" },
  { value: "centred", labelKey: "kpis.wizard.v2.presetCentred" },
  { value: "custom", labelKey: "kpis.wizard.v2.presetCustom" },
] as const;
