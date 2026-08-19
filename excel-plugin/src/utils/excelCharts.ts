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

interface ChartAnnotation {
  measures?: Record<string, { title: string; type: string }>;
  dimensions?: Record<string, { title: string; type: string }>;
  timeDimensions?: Record<string, { title: string; type: string }>;
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
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
): ChartRecommendation {
  const colCount = headers.length;
  const rowCount = rows.length;

  if (rowCount === 0 || colCount < 2) {
    return { chartType: 'columnClustered', confidence: 'low', reason: 'Insufficient data for chart recommendation' };
  }

  const timeColumns = new Set<string>();
  const numericColumns = new Set<string>();
  const stringColumns = new Set<string>();

  if (annotation?.timeDimensions) {
    for (const key of Object.keys(annotation.timeDimensions)) {
      timeColumns.add(key);
    }
  }

  for (let col = 0; col < colCount; col++) {
    const header = headers[col];
    if (timeColumns.has(header)) continue;

    let numericCount = 0;
    let stringCount = 0;
    for (let row = 0; row < Math.min(rowCount, 20); row++) {
      const val = rows[row][col];
      if (typeof val === 'number') {
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

  const hasTimeAxis = timeColumns.size > 0;
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
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
): { chartHeaders: string[]; chartRows: (string | number)[][] } {
  const measureKeys = new Set<string>();
  const dimKeys = new Set<string>();

  if (annotation?.measures) {
    for (const [key, m] of Object.entries(annotation.measures)) {
      measureKeys.add(key);
      measureKeys.add(m.title);
    }
  }
  if (annotation?.dimensions) {
    for (const [key, d] of Object.entries(annotation.dimensions)) {
      dimKeys.add(key);
      dimKeys.add(d.title);
    }
  }
  if (annotation?.timeDimensions) {
    for (const [key, d] of Object.entries(annotation.timeDimensions)) {
      dimKeys.add(key);
      dimKeys.add(d.title);
    }
  }

  const dimIndices: number[] = [];
  const measureIndices: number[] = [];

  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    if (measureKeys.has(h)) {
      measureIndices.push(i);
    } else if (dimKeys.has(h)) {
      dimIndices.push(i);
    } else {
      const sample = rows.slice(0, 20);
      const numCount = sample.filter(r => typeof r[i] === 'number').length;
      if (numCount > sample.length / 2) {
        measureIndices.push(i);
      } else {
        dimIndices.push(i);
      }
    }
  }

  if (dimIndices.length === 0) dimIndices.push(0);
  if (measureIndices.length === 0) {
    for (let i = 0; i < headers.length; i++) {
      if (!dimIndices.includes(i)) measureIndices.push(i);
    }
  }

  const chartHeaders = ['Category', ...measureIndices.map(i => headers[i])];
  const chartRows = rows.map(row => {
    const label = dimIndices.map(i => String(row[i] ?? '')).join(' / ');
    const values = measureIndices.map(i => row[i]);
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
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
    timeDimensions?: Record<string, { title: string; type: string }>;
  },
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
