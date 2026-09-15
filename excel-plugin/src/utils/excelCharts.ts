/// <reference types="office-js" />

export type ChartTypeRecommendation = 'line' | 'columnClustered' | 'barClustered' | 'pie' | 'doughnut';

export interface ChartRecommendation {
  chartType: ChartTypeRecommendation;
  confidence: 'high' | 'medium' | 'low';
  reason: string;
}

const AGENT_CHART_MAP: Record<string, ChartTypeRecommendation | null> = {
  line: 'line',
  multi_line: 'line',
  multi_line_wide: 'line',
  bar: 'columnClustered',
  grouped_bar: 'columnClustered',
  stacked_bar: 'columnClustered',
  h_bar: 'barClustered',
  pie: 'pie',
  kpi: null,
};

export function mapAgentChartType(agentType: string | null | undefined): ChartTypeRecommendation | null {
  if (!agentType) return null;
  return AGENT_CHART_MAP[agentType] ?? null;
}

export function getChartTypeEnum(type: ChartTypeRecommendation): Excel.ChartType {
  switch (type) {
    case 'line': return Excel.ChartType.line;
    case 'columnClustered': return Excel.ChartType.columnClustered;
    case 'barClustered': return Excel.ChartType.barClustered;
    case 'pie': return Excel.ChartType.pie;
    case 'doughnut': return Excel.ChartType.doughnut;
    default: return Excel.ChartType.columnClustered;
  }
}

export interface ChartFieldAnnotation {
  title: string;
  type: string;
}

export interface ChartAnnotation {
  measures?: Record<string, ChartFieldAnnotation>;
  dimensions?: Record<string, ChartFieldAnnotation>;
  timeDimensions?: Record<string, ChartFieldAnnotation>;
}

function annotationKeys(
  fields: Record<string, ChartFieldAnnotation> | undefined,
): Set<string> {
  const keys = new Set<string>();
  for (const [key, field] of Object.entries(fields || {})) {
    keys.add(key);
    keys.add(field.title);
  }
  return keys;
}

function numericValue(value: unknown): number | undefined {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

const INTEGER_TEXT_PATTERN = /^[+-]?\d+$/;
const DECIMAL_TEXT_PATTERN = /^[+-]?(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)$/;
const IDENTIFIER_HEADER_TOKENS = new Set([
  'code',
  'id',
  'identifier',
  'key',
  'no',
  'number',
  'period',
  'postal',
  'postcode',
  'year',
  'zip',
]);

function isLikelyIntegerIdentifierColumn(header: string, values: unknown[]): boolean {
  const headerTokens = header.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  if (headerTokens.some(token => IDENTIFIER_HEADER_TOKENS.has(token))) return true;

  const nonEmptyStringValues = values
    .filter((value): value is string => typeof value === 'string' && value.trim() !== '')
    .map(value => value.trim());
  const integerTextValues = nonEmptyStringValues.filter(value => INTEGER_TEXT_PATTERN.test(value));
  if (integerTextValues.length < 2 || integerTextValues.length !== nonEmptyStringValues.length) {
    return false;
  }

  const lengths = new Set(integerTextValues.map(value => value.replace(/^[+-]/, '').length));
  const [length] = [...lengths];
  return lengths.size === 1 && length >= 4;
}

function unannotatedNumericValue(value: unknown, integerIdentifier = false): number | undefined {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  const trimmed = value.trim();
  if (integerIdentifier && INTEGER_TEXT_PATTERN.test(trimmed)) return undefined;
  if (!INTEGER_TEXT_PATTERN.test(trimmed) && !DECIMAL_TEXT_PATTERN.test(trimmed)) return undefined;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

/**
 * Build the row matrix used by the chart and local-pivot insertion paths.
 *
 * Agent-service queries use the general `/execute` route, whose Decimal values
 * can still cross the JSON boundary as strings. The plugin-execute route is
 * typed after Bug-9876, but this path must remain safe when the Agent sample
 * or a legacy response contains a numeric string. Only annotated measure
 * columns are coerced, so numeric-looking dimension keys retain their text.
 */
export function buildChartRowsFromRecords(
  headers: string[],
  records: Array<Record<string, unknown>>,
  annotation?: ChartAnnotation,
): (string | number)[][] {
  const measureKeys = annotationKeys(annotation?.measures);
  return records.map(record => headers.map(header => {
    const value = record[header];
    if (measureKeys.has(header)) {
      const parsed = numericValue(value);
      if (parsed !== undefined) return parsed;
    }
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string') return value;
    return value == null ? '' : String(value);
  }));
}

/**
 * Bug-7416: the query-router's plugin-execute annotation always returns an
 * empty `timeDimensions` map (`_build_annotation` hardcodes `{}`), so a
 * time-series result was classified as an ordinary categorical dimension and
 * `recommendChartType` never chose a line chart.
 *
 * The plugin already knows which dimensions are time dimensions
 * (`Dimension.is_time_dimension`, loaded for the model). This pure helper
 * reclassifies any annotation `dimensions` entry whose dimension name is a
 * known time dimension into `timeDimensions`, so the downstream chart
 * recommender and axis logic (which already read `timeDimensions`) see the
 * time axis. Idempotent and non-mutating: returns a new annotation object.
 *
 * `timeDimensionNames` is the set of technical dimension names the model marks
 * `is_time_dimension`. Matching is on the annotation KEY (the technical name),
 * not the display title, since the backend keys `dimensions` by name.
 */
export function enrichAnnotationTimeDimensions(
  annotation: ChartAnnotation | undefined,
  timeDimensionNames: Iterable<string>,
): ChartAnnotation | undefined {
  if (!annotation) return annotation;
  const timeNames = new Set(timeDimensionNames);
  if (timeNames.size === 0 || !annotation.dimensions) return annotation;

  const remainingDims: Record<string, { title: string; type: string }> = {};
  const timeDims: Record<string, { title: string; type: string }> = {
    ...(annotation.timeDimensions || {}),
  };
  let moved = false;
  for (const [key, d] of Object.entries(annotation.dimensions)) {
    if (timeNames.has(key)) {
      timeDims[key] = d;
      moved = true;
    } else {
      remainingDims[key] = d;
    }
  }
  if (!moved) return annotation;
  return { ...annotation, dimensions: remainingDims, timeDimensions: timeDims };
}

export function recommendChartType(
  headers: string[],
  rows: (string | number)[][],
  annotation?: ChartAnnotation,
): ChartRecommendation {
  const colCount = headers.length;
  const rowCount = rows.length;

  if (rowCount === 0 || colCount < 2) {
    return { chartType: 'columnClustered', confidence: 'low', reason: 'Insufficient data for chart recommendation' };
  }

  const measureColumns = annotationKeys(annotation?.measures);
  const dimensionColumns = new Set([
    ...annotationKeys(annotation?.dimensions),
    ...annotationKeys(annotation?.timeDimensions),
  ]);
  const timeColumns = annotationKeys(annotation?.timeDimensions);
  const numericColumns = new Set<string>();
  const stringColumns = new Set<string>();

  for (let col = 0; col < colCount; col++) {
    const header = headers[col];
    if (timeColumns.has(header)) continue;

    // The citation-derived annotation is authoritative for Agent rows. It is
    // deliberately checked before the value heuristic because a measure can
    // be serialized as a numeric string on the general Agent execute path.
    if (measureColumns.has(header)) {
      numericColumns.add(header);
      continue;
    }
    if (dimensionColumns.has(header)) {
      stringColumns.add(header);
      continue;
    }
    if (annotation) {
      // An annotation is a partial classification contract: a returned
      // column that is not cited as a measure is a category, even when its
      // values happen to look numeric (years and postal codes are common).
      stringColumns.add(header);
      continue;
    }

    const sample = rows.slice(0, 20);
    const integerIdentifier = isLikelyIntegerIdentifierColumn(
      header,
      sample.map(row => row[col]),
    );
    let numericCount = 0;
    let stringCount = 0;
    for (let row = 0; row < Math.min(rowCount, 20); row++) {
      const val = rows[row][col];
      if (unannotatedNumericValue(val, integerIdentifier) !== undefined) {
        numericCount++;
      } else if (typeof val === 'string' && val.length > 0) {
        stringCount++;
      }
    }
    if (numericCount >= stringCount && numericCount > 0) {
      numericColumns.add(header);
    } else {
      stringColumns.add(header);
    }
  }

  // Keep the recommendation aligned with separateColumns when an unannotated
  // result contains only numeric values: the first column is the category axis.
  // Explicit measure annotations remain authoritative; a measure-only result
  // uses a synthetic category and retains every numeric series.
  if (!annotation && stringColumns.size === 0 && timeColumns.size === 0 && numericColumns.size > 1) {
    const fallbackCategory = headers.find(header => numericColumns.has(header));
    if (fallbackCategory) {
      numericColumns.delete(fallbackCategory);
      stringColumns.add(fallbackCategory);
    }
  }

  const hasTimeAxis = headers.some(header => timeColumns.has(header));
  const numCategories = stringColumns.size + timeColumns.size;
  const numMeasures = numericColumns.size;

  if (hasTimeAxis && numMeasures >= 1) {
    return { chartType: 'line', confidence: 'high', reason: 'Time series detected with date dimension and numeric measures' };
  }

  if (numCategories >= 1 && numMeasures >= 1) {
    if (numCategories === 1 && rowCount <= 8 && numMeasures <= 2) {
      return { chartType: 'pie', confidence: 'medium', reason: 'Few categories with measures suggest part-of-whole view' };
    }
    if (numCategories === 1 && rowCount <= 12 && numMeasures <= 2) {
      return { chartType: 'doughnut', confidence: 'medium', reason: 'Limited categories suitable for ring chart' };
    }
    if (rowCount > 12) {
      return { chartType: 'barClustered', confidence: 'high', reason: 'Many categories work best as horizontal bars' };
    }
    return { chartType: 'columnClustered', confidence: 'high', reason: 'Category + measure data suited for column chart' };
  }

  return { chartType: 'columnClustered', confidence: 'low', reason: 'Default recommendation' };
}

export function separateColumns(
  headers: string[],
  rows: (string | number)[][],
  annotation?: ChartAnnotation,
): { chartHeaders: string[]; chartRows: (string | number)[][] } {
  const measureKeys = annotationKeys(annotation?.measures);
  const dimKeys = new Set([
    ...annotationKeys(annotation?.dimensions),
    ...annotationKeys(annotation?.timeDimensions),
  ]);

  const dimIndices: number[] = [];
  const measureIndices: number[] = [];

  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    if (measureKeys.has(h)) {
      measureIndices.push(i);
    } else if (dimKeys.has(h)) {
      dimIndices.push(i);
    } else if (annotation) {
      // An annotation may omit a returned dimension. Unknown columns remain
      // categories; never infer a measure from their values in this case.
      dimIndices.push(i);
    } else {
      const sample = rows.slice(0, 20);
      // When annotation is unavailable, inspect the actual cell values. Decimal
      // numeric strings are measures even when they contain display zeros, while
      // identifier-shaped integer text remains a category.
      const integerIdentifier = isLikelyIntegerIdentifierColumn(
        h,
        sample.map(row => row[i]),
      );
      const numCount = sample.filter(
        r => unannotatedNumericValue(r[i], integerIdentifier) !== undefined,
      ).length;
      if (numCount > sample.length / 2) {
        measureIndices.push(i);
      } else {
        dimIndices.push(i);
      }
    }
  }

  const syntheticCategory = Boolean(annotation) && dimIndices.length === 0 && measureIndices.length > 0;
  if (dimIndices.length === 0 && !syntheticCategory) {
    // Every column can look numeric in an unannotated answer. Promote the
    // conventional first column to the category axis and remove it from the
    // measures so no index is emitted in both sets.
    const fallbackDimension = measureIndices.shift() ?? 0;
    dimIndices.push(fallbackDimension);
  }
  if (measureIndices.length === 0) {
    for (let i = 0; i < headers.length; i++) {
      if (!dimIndices.includes(i)) measureIndices.push(i);
    }
  }

  const chartHeaders = ['Category', ...measureIndices.map(i => headers[i])];
  const chartRows = rows.map(row => {
    const label = syntheticCategory
      ? 'Result'
      : dimIndices.map(i => String(row[i] ?? '')).join(' / ');
    const values = measureIndices.map(i => numericValue(row[i]) ?? row[i]);
    return [label, ...values] as (string | number)[];
  });

  return { chartHeaders, chartRows };
}

/**
 * Bug-6733: chart creation is split into a critical core (data range, chart
 * object, position, title) and non-critical axis formatting. The core is
 * synced first so the chart exists regardless of whether axis-title writes
 * fail on certain Excel hosts / chart types. The caller
 * (`useExcel.insertChart`) syncs the core, then applies axis formatting in
 * a separate non-fatal sync -- so a post-insert axis error never propagates
 * as "Insert failed" when the chart was actually created.
 */
export function createChartOnSheet(
  chartType: Excel.ChartType,
  sheet: Excel.Worksheet,
  headers: string[],
  rows: (string | number)[][],
  annotation?: ChartAnnotation,
  title?: string,
): { chart: Excel.Chart; chartHeaders: string[] } {
  const { chartHeaders, chartRows } = separateColumns(headers, rows, annotation);

  const dataRowCount = chartRows.length + 1;
  const dataColCount = chartHeaders.length;
  const startRow = rows.length + 3;
  const chartDataRange = sheet.getRangeByIndexes(startRow, 0, dataRowCount, dataColCount);
  chartDataRange.values = [chartHeaders, ...chartRows];

  const chart = sheet.charts.add(chartType, chartDataRange, Excel.ChartSeriesBy.columns);
  const chartTop = startRow + dataRowCount + 1;
  chart.setPosition(`A${chartTop + 1}`, `K${chartTop + 21}`);
  chart.title.text = title || 'Tessallite Result';

  return { chart, chartHeaders };
}

/**
 * Bug-6733: non-critical axis formatting extracted from the chart creation
 * path. If this throws on `context.sync()` (e.g. pie charts that do not
 * support category axes in some Excel hosts), the chart itself is already
 * persisted. Called by `useExcel.insertChart` inside a try-catch after the
 * core chart sync succeeds.
 *
 * R1 Finding 2: `fallbackMeasureHeaders` restores the pre-refactor
 * behaviour where the value-axis title fell back to `chartHeaders.slice(1)`
 * (the actual column names from the data range) when `annotation.measures`
 * is absent. Without it, the Ask-Tessallite and KPI-panel chart paths
 * (which pass no annotation) would show a generic "Value" axis title
 * instead of the real measure name.
 */
export function applyChartAxisFormatting(
  chart: Excel.Chart,
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
  fallbackMeasureHeaders?: string[],
): void {
  const dimNames: string[] = [];
  if (annotation?.dimensions) {
    for (const d of Object.values(annotation.dimensions)) dimNames.push(d.title);
  }
  if (annotation?.timeDimensions) {
    for (const d of Object.values(annotation.timeDimensions)) dimNames.push(d.title);
  }
  const measureNames = annotation?.measures
    ? Object.values(annotation.measures).map(m => m.title)
    : (fallbackMeasureHeaders ?? []);

  const categoryAxis = chart.axes.getItem(Excel.ChartAxisType.category);
  categoryAxis.title.text = dimNames.join(' / ') || 'Category';
  categoryAxis.title.visible = true;

  const valueAxis = chart.axes.getItem(Excel.ChartAxisType.value);
  valueAxis.title.text = measureNames.join(' / ') || 'Value';
  valueAxis.title.visible = true;
}

/**
 * Bug-9737: the Agent conversation turn carries no `annotation` field (unlike
 * the plugin-execute response ReportBuilder reads), so every Ask & Insert
 * chart/pivot call site was omitting `annotation` entirely and falling back
 * to `separateColumns`'s `typeof val === 'number'` heuristic. The Agent API
 * returns measure values as JSON strings (e.g. `"23332917.80"`), which always
 * fails that check -- every column got classified as a dimension, collapsing
 * the chart to one concatenated "Category" column with no measure series
 * (an empty chart). `turn.citations` already carries the real measure/
 * dimension classification per column (`kind`, `name`, `display_name`); this
 * builds the same `{measures, dimensions}` shape the annotation-aware path
 * already handles correctly, keyed by the citation's technical `name` (which
 * matches the `query_result_sample` row keys used as chart headers).
 */
export function buildAnnotationFromCitations(
  citations: Array<{ kind: string; name: string; display_name: string }> | null | undefined,
): ChartAnnotation | undefined {
  if (!citations || citations.length === 0) return undefined;
  const measures: Record<string, { title: string; type: string }> = {};
  const dimensions: Record<string, { title: string; type: string }> = {};
  for (const c of citations) {
    if (c.kind === 'measure') {
      measures[c.name] = { title: c.display_name, type: 'measure' };
    } else if (c.kind === 'dimension') {
      dimensions[c.name] = { title: c.display_name, type: 'dimension' };
    }
  }
  if (Object.keys(measures).length === 0 && Object.keys(dimensions).length === 0) return undefined;
  return { measures, dimensions };
}
