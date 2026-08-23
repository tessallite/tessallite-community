export type AutoChartKind = "bar" | "hbar" | "line" | "pie" | "metric";

export interface AutoChartSeries {
  name: string;
  values: (number | null)[];
}

export interface AutoChartSpec {
  kind: AutoChartKind;
  dimension: string;
  labels: string[];
  series: AutoChartSeries[];
  truncated?: { shown: number; total: number };
}

const MAX_POINTS = 80;
const MAX_LABEL_CARDINALITY = 120;

export function buildAutoChartSpec(
  rows: Record<string, unknown>[],
  maxPoints = MAX_POINTS,
): AutoChartSpec | null {
  if (rows.length === 0) return null;

  const columns = Object.keys(rows[0] ?? {});
  const numericColumns = columns.filter((col) => isMeasureColumn(rows, col));
  if (numericColumns.length === 0) return null;

  if (rows.length === 1) {
    return buildSingleRowSpec(rows[0]!, numericColumns);
  }

  const dimensionColumns = columns.filter(
    (col) => !numericColumns.includes(col) && hasUsefulLabels(rows, col),
  );
  const dimension =
    dimensionColumns[0] ??
    columns.find((col) => hasUsefulLabels(rows, col)) ??
    "__row";

  const filteredRows = rows.filter(
    (row) => dimension === "__row" || row[dimension] != null,
  );
  const compactRows = filteredRows.slice(0, maxPoints);
  if (compactRows.length < 2) return null;

  const labels = compactRows.map((row, index) => {
    if (dimension === "__row") return `Row ${index + 1}`;
    if (dimensionColumns.length > 1) {
      return dimensionColumns.map((col) => formatLabel(row[col])).join(" - ");
    }
    return formatLabel(row[dimension]);
  });
  const usableSeries = numericColumns
    .map((col) => ({
      name: prettifyName(col),
      values: compactRows.map((row) => toNumber(row[col])),
    }))
    .filter((series) =>
      series.values.some((value) => value !== null && value !== 0),
    )
    .slice(0, 4);

  if (usableSeries.length === 0) return null;

  const truncated =
    filteredRows.length > compactRows.length
      ? { shown: compactRows.length, total: filteredRows.length }
      : undefined;

  return {
    kind: chooseChartKind(labels, dimension, usableSeries),
    dimension,
    labels,
    series: usableSeries,
    ...(truncated ? { truncated } : {}),
  };
}

function buildSingleRowSpec(
  row: Record<string, unknown>,
  numericColumns: string[],
): AutoChartSpec | null {
  const usableValues = numericColumns
    .map((col) => ({ label: prettifyName(col), value: toNumber(row[col]) }))
    .filter(
      (item): item is { label: string; value: number } => item.value !== null,
    );

  if (usableValues.length === 0) return null;

  if (usableValues.length === 1) {
    return {
      kind: "metric",
      dimension: usableValues[0]!.label,
      labels: [usableValues[0]!.label],
      series: [
        {
          name: usableValues[0]!.label,
          values: [usableValues[0]!.value],
        },
      ],
    };
  }

  const avgLabelLen =
    usableValues.reduce((sum, item) => sum + item.label.length, 0) /
    usableValues.length;
  return {
    kind: avgLabelLen > 10 || usableValues.length > 5 ? "hbar" : "bar",
    dimension: "Measures",
    labels: usableValues.map((item) => item.label),
    series: [
      {
        name: "Value",
        values: usableValues.map((item) => item.value),
      },
    ],
  };
}

/**
 * Bug-7381: detect whether a column name looks like a year, identifier, or
 * code column (case-insensitive).  These should be treated as dimensions even
 * when their values are native numbers.
 */
const IDENTIFIER_NAME_PATTERN =
  /^(year|yr|month|id|code|zip|postal|fips|sku|upc|ean|isbn)(_|$)/i;
const YEAR_NAME_PATTERN = /year|yr/i;

function _looksLikeYearValues(values: number[]): boolean {
  // A plausible four-digit year range: every value is an integer in
  // [1900, 2200].  This catches {2023, 2024, 2025} without misclassifying
  // revenue or count columns that happen to be near 2000.
  return values.every(
    (v) => Number.isInteger(v) && v >= 1900 && v <= 2200,
  );
}

function isMeasureColumn(
  rows: Record<string, unknown>[],
  column: string,
): boolean {
  const present = rows.map((row) => row[column]).filter((v) => v != null);
  if (present.length === 0) return false;
  if (!present.some((v) => toNumber(v) !== null)) return false;

  // Bug-7381: apply the year/identifier heuristic to native numbers as well
  // as strings.  A column named "year" whose values are {2023, 2024} is a
  // dimension, not a measure.
  if (present.every((v) => typeof v === "number")) {
    // Column name looks like an identifier or code -> dimension.
    if (IDENTIFIER_NAME_PATTERN.test(column)) return false;
    // Column name contains "year" and values are plausible years -> dimension.
    if (YEAR_NAME_PATTERN.test(column) && _looksLikeYearValues(present as number[])) {
      return false;
    }
    // All-integer values in a plausible year range AND column name is not
    // clearly a measure -> dimension.  This catches generic "Year" columns
    // even when the name variant is not in YEAR_NAME_PATTERN.
    if (_looksLikeYearValues(present as number[])) {
      const distinct = new Set(present).size;
      if (distinct <= Math.min(MAX_LABEL_CARDINALITY, rows.length) && distinct < rows.length) {
        return false;
      }
    }
    return true;
  }

  const stringVals = present.filter(
    (v) => typeof v === "string",
  ) as string[];
  if (stringVals.length > 0) {
    const allYearLike = stringVals.every((v) => /^\d{4}$/.test(v.trim()));
    if (allYearLike) return false;
    const allInteger = stringVals.every((v) => /^-?\d+$/.test(v.trim()));
    const distinct = new Set(stringVals.map((v) => v.trim())).size;
    if (
      allInteger &&
      distinct <= Math.min(MAX_LABEL_CARDINALITY, rows.length) &&
      distinct < rows.length
    ) {
      return false;
    }
  }
  return true;
}

function hasUsefulLabels(
  rows: Record<string, unknown>[],
  column: string,
): boolean {
  const values = rows
    .map((row) => row[column])
    .filter((value) => value != null);
  if (values.length < 2) return false;
  const unique = new Set(values.map(formatLabel));
  return (
    unique.size > 1 &&
    unique.size <= Math.min(MAX_LABEL_CARDINALITY, rows.length)
  );
}

function chooseChartKind(
  labels: string[],
  dimension: string,
  series: AutoChartSeries[],
): AutoChartKind {
  if (
    series.length === 1 &&
    labels.length <= 8 &&
    !looksTemporal(dimension, labels) &&
    isAllNonNegative(series[0]!)
  ) {
    return "pie";
  }
  if (looksTemporal(dimension, labels)) return "line";
  const avgLen =
    labels.reduce((sum, l) => sum + l.length, 0) / labels.length;
  if (labels.length <= 20 && avgLen > 12) return "hbar";
  return "bar";
}

function isAllNonNegative(series: AutoChartSeries): boolean {
  return series.values.every((v) => v === null || v >= 0);
}

function looksTemporal(dimension: string, labels: string[]): boolean {
  if (/date|time|month|year|week|day|period/i.test(dimension)) return true;
  return labels.slice(0, 5).every((label) => !Number.isNaN(Date.parse(label)));
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[$,%\s,]/g, "");
  if (!cleaned) return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatLabel(value: unknown): string {
  if (value == null) return "";
  if (value instanceof Date) return value.toLocaleDateString();
  return String(value);
}

function prettifyName(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}
