import type { MeasureFormatToken } from "./types";
import { DISPLAY_CURRENCY, DISPLAY_LOCALE } from "./displayConfig";

export const MEASURE_FORMAT_TOKENS: MeasureFormatToken[] = [
  "currency",
  "percent",
  "percent_2dp",
  "integer",
  "decimal_2dp",
  "decimal_0",
  "decimal_1",
  "decimal_3",
  "decimal_4",
  "decimal_5",
  "decimal_6",
];

export const MEASURE_FORMAT_LABELS: Record<MeasureFormatToken, string> = {
  currency: "measureFormat.currency",
  percent: "measureFormat.percent",
  percent_2dp: "measureFormat.percent2dp",
  integer: "measureFormat.integer",
  decimal_2dp: "measureFormat.decimal2dp",
  decimal_0: "measureFormat.decimal0",
  decimal_1: "measureFormat.decimal1",
  decimal_3: "measureFormat.decimal3",
  decimal_4: "measureFormat.decimal4",
  decimal_5: "measureFormat.decimal5",
  decimal_6: "measureFormat.decimal6",
};

const DECIMAL_DP: Partial<Record<MeasureFormatToken, number>> = {
  decimal_0: 0,
  decimal_1: 1,
  decimal_2dp: 2,
  decimal_3: 3,
  decimal_4: 4,
  decimal_5: 5,
  decimal_6: 6,
};

function formatGrouped(value: number, dp: number): string {
  return new Intl.NumberFormat(DISPLAY_LOCALE, {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  }).format(value);
}

export function formatMeasureValue(
  value: unknown,
  token: MeasureFormatToken | null | undefined,
): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) {
    return String(value);
  }
  if (!token) {
    return String(value);
  }

  switch (token) {
    case "currency":
      return new Intl.NumberFormat(DISPLAY_LOCALE, {
        style: "currency",
        currency: DISPLAY_CURRENCY,
      }).format(num);
    // F-015-07: the `percent` token's declared convention (matching the
    // canonical KPI formatter, model-service kpi_formatter.py:128-130) is
    // "value is a decimal ratio — multiply by 100". The old magnitude
    // heuristic (× 100 only when |num| < 1) flipped meaning at 1.0: a 101%
    // ratio (1.01) rendered as "1%" and yoy_growth_pct of +150% (1.5)
    // rendered as "2%". Always scale ratios; never branch on magnitude.
    case "percent":
      return `${formatGrouped(num * 100, 0)}%`;
    case "percent_2dp":
      return `${formatGrouped(num * 100, 2)}%`;
    case "integer":
      return formatGrouped(num, 0);
    default: {
      const dp = DECIMAL_DP[token];
      if (dp === undefined) {
        return String(value);
      }
      return formatGrouped(num, dp);
    }
  }
}
