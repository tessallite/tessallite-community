// Mirrors tessallite/shared/schemas/measure_formats.py.
// Keep in sync when adding or renaming variants.

export const TIME_VARIANT_NAMES = [
  "lag",
  "prior_year",
  "prior_quarter",
  "prior_month",
  "prior_week",
  "ytd",
  "qtd",
  "mtd",
  "wtd",
  "ytd_prior_year",
  "yoy_growth",
  "yoy_growth_pct",
  "trailing_n",
  "moving_avg_n",
  "last_n_periods",
  "period_to_date",
  "same_period_last_year",
] as const;

export type TimeVariantKind = (typeof TIME_VARIANT_NAMES)[number];

// F-015-13: the catalog-admitted, documented canon (14). The picker offers
// only these so the drawer never presents semantic duplicates ("YTD" and
// "Period to date"). TIME_VARIANT_NAMES (above, 17) is retained for label
// lookup of alias kinds carried by historical rows. Mirrors
// shared/schemas/measure_formats.py CANONICAL_TIME_VARIANT_ORDER.
export const CANONICAL_TIME_VARIANT_NAMES = [
  "lag",
  "prior_year",
  "prior_quarter",
  "prior_month",
  "prior_week",
  "ytd",
  "qtd",
  "mtd",
  "wtd",
  "ytd_prior_year",
  "yoy_growth",
  "yoy_growth_pct",
  "trailing_n",
  "moving_avg_n",
] as const;

export const TIME_VARIANTS_NEEDING_CALENDAR: ReadonlySet<TimeVariantKind> =
  new Set([
    "prior_year",
    "prior_quarter",
    "prior_month",
    "prior_week",
    "ytd",
    "qtd",
    "mtd",
    "wtd",
    "ytd_prior_year",
    "yoy_growth",
    "yoy_growth_pct",
    "period_to_date",
    "same_period_last_year",
  ]);

export const TIME_VARIANT_PARAMETRIC: ReadonlySet<TimeVariantKind> = new Set([
  "trailing_n",
  "moving_avg_n",
  "last_n_periods",
]);

// F-015-24: variant kinds that emit a percentage/ratio rather than the base
// measure's natural unit. Their result is a decimal ratio (e.g. 0.5 = +50%),
// so the catalog should default them to a percent format rather than inheriting
// the base measure's format (e.g. currency). The modeler can still override.
// Mirrors the percent-producing handlers in
// shared/semantic/time_variants_sql.py (_h_yoy_growth_pct / _h_cagr /
// _h_pct_change). Plain `yoy_growth` is an absolute delta in the base unit and
// is deliberately excluded.
export const RATIO_VARIANT_KINDS: ReadonlySet<string> = new Set([
  "yoy_growth_pct",
  "cagr",
  "pct_change",
]);

// F-015-24: the default format token applied to a ratio variant when the
// modeler has not chosen one. "percent" renders the stored decimal ×100 with a
// % sign (see api/measureFormat.ts, F-015-07 convention).
export const RATIO_VARIANT_DEFAULT_FORMAT = "percent" as const;

export function isRatioVariant(kind: string): boolean {
  return RATIO_VARIANT_KINDS.has(kind);
}

export const TIME_VARIANT_DEFAULT_N: Record<string, number> = {
  trailing_n: 12,
  moving_avg_n: 30,
  last_n_periods: 12,
};

export function isParametricVariant(kind: string): boolean {
  return TIME_VARIANT_PARAMETRIC.has(kind as TimeVariantKind);
}

export const TIME_VARIANT_LABELS: Record<string, string> = {
  lag: "timeVariants.lag",
  prior_year: "timeVariants.priorYear",
  prior_quarter: "timeVariants.priorQuarter",
  prior_month: "timeVariants.priorMonth",
  prior_week: "timeVariants.priorWeek",
  ytd: "timeVariants.ytd",
  qtd: "timeVariants.qtd",
  mtd: "timeVariants.mtd",
  wtd: "timeVariants.wtd",
  ytd_prior_year: "timeVariants.ytdPriorYear",
  yoy_growth: "timeVariants.yoyGrowth",
  yoy_growth_pct: "timeVariants.yoyGrowthPct",
  trailing_n: "timeVariants.trailingN",
  moving_avg_n: "timeVariants.movingAvgN",
  last_n_periods: "timeVariants.lastNPeriods",
  period_to_date: "timeVariants.periodToDate",
  same_period_last_year: "timeVariants.samePeriodLastYear",
};
